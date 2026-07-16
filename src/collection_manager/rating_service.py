from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from collection_manager.constants import (
    TIER_ASCENDING,
    CollectionKind,
    HeavyStatus,
    PointEventKind,
    RuleKind,
    Tier,
)
from collection_manager.domain import RuleSuggestion, TierShiftProposal
from collection_manager.models import Artist, PointEvent, RuleEffect, TierEvent, utc_now
from collection_manager.repository import ArtistNotFoundError, ArtistRepository


class RatingError(ValueError):
    """Base exception for an invalid rating operation."""


class NoTierShiftError(RatingError):
    """Raised when an artist does not currently have a legal tier shift."""


class StaleRuleSuggestionError(RatingError):
    """Raised when a rule suggestion no longer matches current artist state."""


class EventNotReversibleError(RatingError):
    """Raised when reversing an event would make the audit ledger inconsistent."""


@dataclass(slots=True)
class _Action:
    point_event_ids: tuple[int, ...] = ()
    tier_event_ids: tuple[int, ...] = ()
    artist_ids: tuple[int, ...] = ()
    previous_update_dates: tuple[tuple[int, date | None], ...] = ()


class RatingService:
    """Audited point, rule, and tier workflows.

    Mutations are flushed but not committed. A service instance also retains a lightweight
    in-memory action stack so Ctrl+Z can undo a multi-event operation (such as a tier shift) as
    one action. ``undo_last`` falls back to database history after an application restart.
    """

    HEAVY_RULE_KEY = "heavy:uncompressed"
    DIVERSITY_RULE_KEY = "diversity:5-tags"

    def __init__(self, session: Session):
        self.session = session
        self.artists = ArtistRepository(session)
        self._actions: list[_Action] = []

    def log_update(
        self,
        artist_id: int,
        update_date: date,
        sentiment: str | bool,
        reason: str = "",
    ) -> PointEvent:
        """Log an explicitly dated good (+1) or bad (-1) update."""

        if update_date is None or not isinstance(update_date, date):
            raise RatingError("An explicit update date is required")
        if isinstance(update_date, datetime):
            update_date = update_date.date()
        delta, label = self._sentiment_delta(sentiment)
        artist = self._active_artist(artist_id)
        previous_update_date = artist.last_updated
        event = self._record_adjustment(
            artist,
            delta=delta,
            reason=reason.strip() or f"{label} update",
            kind=PointEventKind.UPDATE_VIBE,
        )
        artist.last_updated = update_date
        self.session.flush()
        self._remember(
            point_events=[event],
            artist_ids=[artist.id],
            previous_update_dates=[(artist.id, previous_update_date)],
        )
        return event

    log_vibe = log_update

    def adjust_points(
        self,
        artist_id: int,
        delta: int,
        reason: str,
        *,
        kind: PointEventKind | str = PointEventKind.MANUAL_ADJUSTMENT,
        rule_key: str | None = None,
        mark_attention: bool = False,
    ) -> PointEvent:
        """Apply a nonzero point adjustment and append an audit event."""

        if not isinstance(delta, int) or isinstance(delta, bool) or delta == 0:
            raise RatingError("Point adjustment delta must be a nonzero integer")
        if not reason.strip():
            raise RatingError("A point adjustment reason is required")
        event_kind = kind if isinstance(kind, PointEventKind) else PointEventKind(kind)
        if event_kind is PointEventKind.TIER_RESET:
            raise RatingError("Tier-reset events are reserved for tier workflows")
        artist = self._active_artist(artist_id)
        event = self._record_adjustment(
            artist,
            delta=delta,
            reason=reason.strip(),
            kind=kind,
            rule_key=rule_key,
        )
        if mark_attention and self.propose_tier_shift(artist.id) is not None:
            event.pending_tier_shift = True
        self.session.flush()
        self._remember(point_events=[event], artist_ids=[artist.id])
        return event

    def evaluate_rules(
        self,
        as_of_date: date | None = None,
        artist_ids: Iterable[int] | None = None,
        collection_kind: CollectionKind | str | None = None,
    ) -> list[RuleSuggestion]:
        """Return current, unapplied adjustments without mutating artist points."""

        today = as_of_date or date.today()
        if isinstance(today, datetime):
            today = today.date()
        if not isinstance(today, date):
            raise RatingError("as_of_date must be a date")

        statement = (
            select(Artist)
            .options(
                selectinload(Artist.tags),
                selectinload(Artist.rule_effects),
            )
            .where(Artist.deleted_at.is_(None))
            .order_by(Artist.name_key.asc(), Artist.id.asc())
            .execution_options(populate_existing=True)
        )
        if collection_kind is not None:
            statement = statement.where(
                Artist.collection_kind == CollectionKind(collection_kind).value
            )
        if artist_ids is not None:
            ids = tuple(dict.fromkeys(int(value) for value in artist_ids))
            if not ids:
                return []
            statement = statement.where(Artist.id.in_(ids))

        suggestions: list[RuleSuggestion] = []
        for artist in self.session.scalars(statement).unique():
            desired = self._desired_rule_adjustments(artist, today)
            effects = {effect.rule_key: effect for effect in artist.rule_effects}

            for rule_key, (rule_kind, delta, reason) in desired.items():
                effect = effects.get(rule_key)
                if effect is None or not effect.active:
                    suggestions.append(
                        RuleSuggestion(
                            artist_id=artist.id,
                            artist_name=artist.name,
                            rule_kind=rule_kind,
                            rule_key=rule_key,
                            delta=delta,
                            reason=reason,
                        )
                    )

            for effect in sorted(artist.rule_effects, key=lambda item: item.rule_key):
                if not effect.active or effect.rule_key in desired:
                    continue
                try:
                    rule_kind = RuleKind(effect.rule_kind)
                except ValueError:
                    # Unknown effects belong to a newer/third-party rule and are left alone.
                    continue
                suggestions.append(
                    RuleSuggestion(
                        artist_id=artist.id,
                        artist_name=artist.name,
                        rule_kind=rule_kind,
                        rule_key=effect.rule_key,
                        delta=-effect.applied_delta,
                        reason=self._reversal_reason(effect),
                        is_reversal=True,
                    )
                )
        return suggestions

    def apply_rule_suggestions(
        self,
        suggestions: Iterable[RuleSuggestion],
        as_of_date: date | None = None,
    ) -> list[PointEvent]:
        """Apply a reviewed subset as one auditable batch.

        The supplied objects must still be present in a fresh evaluation. This prevents a stale
        preview from applying after the artist's dates, tags, or heavy/compression state changed.
        Any legal threshold reached by the final batch balance is deferred to Attention Needed.
        """

        selected = list(suggestions)
        if not selected:
            return []
        identities = [self._suggestion_identity(item) for item in selected]
        if len(set(identities)) != len(identities):
            raise RatingError("The same rule suggestion was selected more than once")

        artist_ids = tuple(dict.fromkeys(item.artist_id for item in selected))
        current = self.evaluate_rules(as_of_date, artist_ids)
        current_by_identity = {self._suggestion_identity(item): item for item in current}
        stale = [
            item for item in selected if self._suggestion_identity(item) not in current_by_identity
        ]
        if stale:
            labels = ", ".join(f"{item.artist_name}: {item.rule_key}" for item in stale)
            raise StaleRuleSuggestionError(f"Rule suggestion is no longer current: {labels}")

        # Apply the freshly evaluated values, not user-constructed labels/reasons from a stale UI
        # object that merely happens to share the same identity.
        selected = [current_by_identity[self._suggestion_identity(item)] for item in selected]

        events: list[PointEvent] = []
        events_by_artist: dict[int, list[PointEvent]] = {}
        for suggestion in selected:
            artist = self._active_artist(suggestion.artist_id)
            event = self._record_adjustment(
                artist,
                delta=suggestion.delta,
                reason=suggestion.reason,
                kind=PointEventKind.RULE_ADJUSTMENT,
                rule_key=suggestion.rule_key,
            )
            effects = {
                effect.rule_key: effect
                for effect in self.session.scalars(
                    select(RuleEffect).where(RuleEffect.artist_id == artist.id)
                )
            }
            effect = effects.get(suggestion.rule_key)
            if suggestion.is_reversal:
                if effect is None or not effect.active:
                    raise StaleRuleSuggestionError(
                        f"Rule effect {suggestion.rule_key!r} is no longer active"
                    )
                original = self.session.get(PointEvent, effect.point_event_id)
                if original is not None:
                    original.reversed_event_id = event.id
                effect.active = False
                effect.reversal_point_event_id = event.id
                effect.reversed_at = utc_now()
            elif effect is None:
                effect = RuleEffect(
                    artist_id=artist.id,
                    rule_kind=suggestion.rule_kind.value,
                    rule_key=suggestion.rule_key,
                    applied_delta=suggestion.delta,
                    active=True,
                    point_event_id=event.id,
                )
                self.session.add(effect)
            else:
                effect.rule_kind = suggestion.rule_kind.value
                effect.applied_delta = suggestion.delta
                effect.active = True
                effect.point_event_id = event.id
                effect.reversal_point_event_id = None
                effect.created_at = utc_now()
                effect.reversed_at = None
            events.append(event)
            events_by_artist.setdefault(artist.id, []).append(event)

        self.session.flush()
        evaluated_on = as_of_date or date.today()
        if isinstance(evaluated_on, datetime):
            evaluated_on = evaluated_on.date()
        for artist_id, artist_events in events_by_artist.items():
            self._active_artist(artist_id).date_evaluated = evaluated_on
            if self.propose_tier_shift(artist_id) is not None:
                artist_events[-1].pending_tier_shift = True
        self.session.flush()
        self._remember(point_events=events, artist_ids=events_by_artist)
        return events

    apply_suggestions = apply_rule_suggestions

    def propose_tier_shift(
        self,
        artist_id: int,
        *,
        points: int | None = None,
    ) -> TierShiftProposal | None:
        """Return the one legal threshold shift, or ``None`` at non-threshold/boundary states."""

        artist = self._active_artist(artist_id)
        balance = artist.points if points is None else int(points)
        destination = self._tier_destination(artist.tier_value, balance)
        if destination is None:
            return None
        return TierShiftProposal(
            artist_id=artist.id,
            artist_name=artist.name,
            old_tier=artist.tier_value,
            new_tier=destination,
            points=balance,
        )

    def approve_tier_shift(
        self,
        artist_id: int,
        reason: str = "Rating threshold reached",
    ) -> TierEvent:
        proposal = self.propose_tier_shift(artist_id)
        if proposal is None:
            raise NoTierShiftError("The artist does not have a legal pending tier shift")
        if not reason.strip():
            raise RatingError("A tier shift reason is required")

        artist = self._active_artist(artist_id)
        trigger = self._latest_balance_event(artist)
        old_points = artist.points
        tier_event = TierEvent(
            artist_id=artist.id,
            old_tier=proposal.old_tier.value,
            new_tier=proposal.new_tier.value,
            reason=reason.strip(),
            points_before_reset=old_points,
            triggering_point_event_id=trigger.id if trigger else None,
        )
        self.session.add(tier_event)
        artist.tier = proposal.new_tier.value
        reset = self._record_adjustment(
            artist,
            delta=-old_points,
            reason=f"Points reset after tier shift to {proposal.new_tier.value}",
            kind=PointEventKind.TIER_RESET,
            allow_zero=True,
        )
        self._clear_attention(artist.id)
        self.session.flush()
        self._remember(point_events=[reset], tier_events=[tier_event], artist_ids=[artist.id])
        return tier_event

    approve_shift = approve_tier_shift

    def defer_tier_shift(self, artist_id: int) -> TierShiftProposal:
        proposal = self.propose_tier_shift(artist_id)
        if proposal is None:
            raise NoTierShiftError("The artist does not have a legal tier shift to defer")
        artist = self._active_artist(artist_id)
        event = self._latest_balance_event(artist)
        if event is None:
            # Imported/opening balances normally have an event. Preserve Attention Needed even
            # for legacy databases that predate the ledger by recording a zero-delta marker.
            event = self._record_adjustment(
                artist,
                delta=0,
                reason="Tier shift deferred",
                kind=PointEventKind.MANUAL_ADJUSTMENT,
                allow_zero=True,
            )
        event.pending_tier_shift = True
        self.session.flush()
        return proposal

    defer_shift = defer_tier_shift

    def manual_tier_override(
        self,
        artist_id: int,
        new_tier: Tier | str,
        reason: str,
    ) -> TierEvent:
        if not reason.strip():
            raise RatingError("A manual tier override reason is required")
        artist = self._active_artist(artist_id)
        destination = Tier(new_tier)
        if destination is artist.tier_value:
            raise RatingError("The manual tier override must select a different tier")

        old_tier = artist.tier_value
        old_points = artist.points
        tier_event = TierEvent(
            artist_id=artist.id,
            old_tier=old_tier.value,
            new_tier=destination.value,
            reason=reason.strip(),
            points_before_reset=old_points,
        )
        self.session.add(tier_event)
        artist.tier = destination.value
        reset = self._record_adjustment(
            artist,
            delta=-old_points,
            reason=f"Points reset after manual tier override: {reason.strip()}",
            kind=PointEventKind.TIER_RESET,
            allow_zero=True,
        )
        self._clear_attention(artist.id)
        self.session.flush()
        self._remember(point_events=[reset], tier_events=[tier_event], artist_ids=[artist.id])
        return tier_event

    def reverse_event(self, event_id: int, reason: str = "Undo") -> PointEvent | TierEvent:
        """Reverse the latest point event, keeping tier/reset pairs atomic."""

        if not reason.strip():
            raise RatingError("A reversal reason is required")
        event = self.session.get(PointEvent, event_id)
        if event is None:
            raise EventNotReversibleError(f"Point event {event_id} was not found")
        if event.reversed_event_id is not None:
            raise EventNotReversibleError("The point event has already been reversed")
        self._assert_latest_balance_event(event)
        if event.kind == PointEventKind.TIER_RESET.value:
            tier_event = self._paired_tier_event(event)
            if tier_event is None:
                raise EventNotReversibleError(
                    "A tier-reset event can only be reversed with its paired tier event"
                )
            reversal = self._reverse_tier_event(tier_event, (event.id,), reason.strip())
        else:
            reversal = self._reverse_point_event(event, reason.strip())
        self._discard_actions_for_event(event.id)
        self.session.flush()
        return reversal

    def undo_last_session_action(
        self,
        artist_id: int | None = None,
        reason: str = "Undo",
    ) -> PointEvent | TierEvent | None:
        """Undo only work recorded by this service instance.

        Unlike ``undo_last``, this never guesses from persisted history. UI Ctrl+Z should prefer
        this method so restarting the application cannot expose a previous session's event as a
        newly undoable action.
        """

        if not reason.strip():
            raise RatingError("An undo reason is required")
        action_index = self._last_action_index(artist_id)
        if action_index is None:
            return None
        action = self._actions[action_index]
        result = self._undo_action(action, reason.strip())
        self._actions.pop(action_index)
        self.session.flush()
        return result

    def undo_last(
        self,
        artist_id: int | None = None,
        reason: str = "Undo",
    ) -> PointEvent | TierEvent | None:
        """Undo the most recent service action, preserving reversal events in both ledgers."""

        result = self.undo_last_session_action(artist_id, reason)
        if result is not None:
            return result
        return self._undo_from_history(artist_id, reason.strip())

    def _undo_action(self, action: _Action, reason: str) -> PointEvent | TierEvent | None:
        if action.tier_event_ids:
            # A tier operation owns its reset event; reverse it as one semantic action.
            tier_event = self.session.get(TierEvent, action.tier_event_ids[-1])
            if tier_event is None:
                raise EventNotReversibleError("The tier event no longer exists")
            return self._reverse_tier_event(tier_event, action.point_event_ids, reason)

        result: PointEvent | None = None
        reversed_ids = tuple(reversed(action.point_event_ids))
        for position, event_id in enumerate(reversed_ids):
            event = self.session.get(PointEvent, event_id)
            if event is None:
                raise EventNotReversibleError(f"Point event {event_id} no longer exists")
            result = self._reverse_point_event(
                event,
                reason,
                enforce_latest=position == 0,
            )
        for saved_artist_id, previous_date in action.previous_update_dates:
            self.artists.get(saved_artist_id, include_deleted=True).last_updated = previous_date
        return result

    def _undo_from_history(
        self, artist_id: int | None, reason: str
    ) -> PointEvent | TierEvent | None:
        point_event = self._latest_unreversed_balance_event(artist_id)
        if point_event is None:
            return None

        # Persisted reversal events may be one half of a compound undo. Do not infer a redo from
        # them; the explicit session-only API is the safe UI path.
        if point_event.kind == PointEventKind.REVERSAL.value:
            return None

        if point_event.kind == PointEventKind.TIER_RESET.value:
            tier_event = self._paired_tier_event(point_event)
            if tier_event is None:
                raise EventNotReversibleError(
                    "A tier-reset event can only be reversed with its paired tier event"
                )
            result = self._reverse_tier_event(tier_event, (point_event.id,), reason)
            self.session.flush()
            return result

        result = self._reverse_point_event(point_event, reason)
        self.session.flush()
        return result

    def _reverse_point_event(
        self,
        event: PointEvent,
        reason: str,
        *,
        enforce_latest: bool = True,
    ) -> PointEvent:
        if event.reversed_event_id is not None:
            raise EventNotReversibleError("The point event has already been reversed")
        if event.kind == PointEventKind.TIER_RESET.value:
            raise EventNotReversibleError(
                "A tier-reset event must be reversed with its paired tier event"
            )
        if enforce_latest:
            self._assert_latest_balance_event(event)
        artist = self.artists.get(event.artist_id, include_deleted=True)
        if artist.points != event.new_points:
            raise EventNotReversibleError(
                "Only the latest balance-changing event can be reversed safely"
            )
        reversal = self._record_adjustment(
            artist,
            delta=-event.delta,
            reason=f"{reason.strip()}: {event.reason}",
            kind=PointEventKind.REVERSAL,
            rule_key=event.rule_key,
            allow_zero=True,
        )
        event.reversed_event_id = reversal.id
        event.pending_tier_shift = False

        if event.rule_key:
            effect = self.session.scalar(
                select(RuleEffect).where(
                    RuleEffect.artist_id == event.artist_id,
                    RuleEffect.rule_key == event.rule_key,
                )
            )
            if effect is not None:
                if effect.active and effect.point_event_id == event.id:
                    effect.active = False
                    effect.reversal_point_event_id = reversal.id
                    effect.reversed_at = utc_now()
                elif not effect.active and effect.reversal_point_event_id == event.id:
                    effect.active = True
                    effect.reversal_point_event_id = None
                    effect.reversed_at = None
        self._clear_attention(artist.id)
        if self.propose_tier_shift(artist.id) is not None:
            reversal.pending_tier_shift = True
        return reversal

    def _reverse_tier_event(
        self,
        tier_event: TierEvent,
        reset_event_ids: Sequence[int],
        reason: str,
    ) -> TierEvent:
        if tier_event.reversed_event_id is not None:
            raise EventNotReversibleError("The tier event has already been reversed")
        artist = self.artists.get(tier_event.artist_id, include_deleted=True)
        if artist.tier != tier_event.new_tier or artist.points != 0:
            raise EventNotReversibleError("Only the latest tier change can be reversed safely")

        reset_event = None
        for event_id in reversed(tuple(reset_event_ids)):
            candidate = self.session.get(PointEvent, event_id)
            if candidate is not None and candidate.kind == PointEventKind.TIER_RESET.value:
                reset_event = candidate
                break
        if (
            reset_event is None
            or reset_event.reversed_event_id is not None
            or reset_event.old_points != tier_event.points_before_reset
            or reset_event.new_points != 0
        ):
            raise EventNotReversibleError(
                "A tier change can only be reversed with its paired reset event"
            )
        self._assert_latest_balance_event(reset_event)

        reversal_tier = TierEvent(
            artist_id=artist.id,
            old_tier=tier_event.new_tier,
            new_tier=tier_event.old_tier,
            reason=f"{reason}: {tier_event.reason}",
            points_before_reset=artist.points,
        )
        self.session.add(reversal_tier)
        self.session.flush()
        tier_event.reversed_event_id = reversal_tier.id
        artist.tier = tier_event.old_tier
        restored_points = tier_event.points_before_reset
        point_reversal = self._record_adjustment(
            artist,
            delta=restored_points - artist.points,
            reason=f"{reason}: restore points from tier change",
            kind=PointEventKind.REVERSAL,
            allow_zero=True,
        )
        reset_event.reversed_event_id = point_reversal.id
        reset_event.pending_tier_shift = False
        self._clear_attention(artist.id)
        if self.propose_tier_shift(artist.id) is not None:
            point_reversal.pending_tier_shift = True
        return reversal_tier

    def _record_adjustment(
        self,
        artist: Artist,
        *,
        delta: int,
        reason: str,
        kind: PointEventKind | str,
        rule_key: str | None = None,
        allow_zero: bool = False,
    ) -> PointEvent:
        if delta == 0 and not allow_zero:
            raise RatingError("Zero-delta point events are reserved for workflow markers")
        # A pending proposal represents a particular balance. Any subsequent mutation makes
        # that marker stale; batch workflows mark the final event again when still eligible.
        self._clear_attention(artist.id)
        old_points = artist.points
        new_points = old_points + delta
        event = PointEvent(
            artist_id=artist.id,
            kind=kind.value if isinstance(kind, PointEventKind) else PointEventKind(kind).value,
            delta=delta,
            reason=reason,
            old_points=old_points,
            new_points=new_points,
            rule_key=rule_key,
            pending_tier_shift=False,
        )
        artist.points = new_points
        self.session.add(event)
        self.session.flush()
        return event

    def _desired_rule_adjustments(
        self, artist: Artist, as_of_date: date
    ) -> dict[str, tuple[RuleKind, int, str]]:
        desired: dict[str, tuple[RuleKind, int, str]] = {}
        if artist.last_updated is not None and artist.last_updated <= as_of_date:
            for anniversary in self._full_anniversaries(artist.last_updated, as_of_date):
                key = f"inactivity:{anniversary.isoformat()}"
                desired[key] = (
                    RuleKind.INACTIVITY,
                    -1,
                    f"No update for a full year as of {anniversary.isoformat()}",
                )

        if artist.heavy_status == HeavyStatus.YES.value and not artist.is_compressed:
            desired[self.HEAVY_RULE_KEY] = (
                RuleKind.HEAVY,
                -1,
                "Heavy artist folder is not compressed",
            )

        distinct_tags = {link.tag.name_key for link in artist.tags}
        if len(distinct_tags) >= 5:
            desired[self.DIVERSITY_RULE_KEY] = (
                RuleKind.DIVERSITY,
                1,
                "Artist has five or more distinct tags",
            )
        return desired

    @staticmethod
    def _full_anniversaries(start: date, end: date) -> list[date]:
        anniversaries: list[date] = []
        for year in range(start.year + 1, end.year + 1):
            try:
                anniversary = start.replace(year=year)
            except ValueError:
                # Feb 29 reaches its full anniversary on Feb 28 in non-leap years.
                anniversary = date(year, 2, 28)
            if anniversary <= end:
                anniversaries.append(anniversary)
        return anniversaries

    @staticmethod
    def _reversal_reason(effect: RuleEffect) -> str:
        if effect.rule_kind == RuleKind.HEAVY.value:
            return "Reverse heavy-folder penalty because the condition cleared"
        if effect.rule_kind == RuleKind.DIVERSITY.value:
            return "Reverse tag-diversity bonus because the condition cleared"
        return "Reverse inactivity penalty because the anniversary is no longer applicable"

    @staticmethod
    def _tier_destination(current: Tier, points: int) -> Tier | None:
        if points >= 3:
            if current is Tier.CREAM:
                return None
            index = TIER_ASCENDING.index(current)
            return TIER_ASCENDING[index + 1]
        if points <= -3:
            if current is Tier.BORING:
                return None
            if current is Tier.BANGERS:
                return Tier.FELL_OFF
            index = TIER_ASCENDING.index(current)
            return TIER_ASCENDING[index - 1]
        return None

    def _latest_balance_event(self, artist: Artist) -> PointEvent | None:
        return self.session.scalar(
            select(PointEvent)
            .where(
                PointEvent.artist_id == artist.id,
                PointEvent.new_points == artist.points,
                PointEvent.reversed_event_id.is_(None),
            )
            .order_by(PointEvent.created_at.desc(), PointEvent.id.desc())
            .limit(1)
        )

    def _latest_unreversed_balance_event(self, artist_id: int | None = None) -> PointEvent | None:
        statement = select(PointEvent).where(PointEvent.reversed_event_id.is_(None))
        if artist_id is not None:
            statement = statement.where(PointEvent.artist_id == artist_id)
        return self.session.scalar(
            statement.order_by(PointEvent.created_at.desc(), PointEvent.id.desc()).limit(1)
        )

    def _assert_latest_balance_event(self, event: PointEvent) -> None:
        latest = self._latest_unreversed_balance_event(event.artist_id)
        if latest is None or latest.id != event.id:
            raise EventNotReversibleError(
                "Only the actual latest unreversed balance event can be reversed safely"
            )

    def _paired_tier_event(self, reset_event: PointEvent) -> TierEvent | None:
        if (
            reset_event.kind != PointEventKind.TIER_RESET.value
            or reset_event.new_points != 0
            or not reset_event.reason.startswith("Points reset after ")
        ):
            return None
        artist = self.artists.get(reset_event.artist_id, include_deleted=True)
        return self.session.scalar(
            select(TierEvent)
            .where(
                TierEvent.artist_id == reset_event.artist_id,
                TierEvent.new_tier == artist.tier,
                TierEvent.points_before_reset == reset_event.old_points,
                TierEvent.reversed_event_id.is_(None),
            )
            .order_by(TierEvent.created_at.desc(), TierEvent.id.desc())
            .limit(1)
        )

    def _discard_actions_for_event(self, event_id: int) -> None:
        self._actions = [
            action for action in self._actions if event_id not in action.point_event_ids
        ]

    def _clear_attention(self, artist_id: int) -> None:
        for event in self.session.scalars(
            select(PointEvent).where(
                PointEvent.artist_id == artist_id,
                PointEvent.pending_tier_shift.is_(True),
            )
        ):
            event.pending_tier_shift = False

    def _active_artist(self, artist_id: int) -> Artist:
        try:
            return self.artists.get(artist_id)
        except ArtistNotFoundError as exc:
            raise RatingError(str(exc)) from exc

    def _remember(
        self,
        *,
        point_events: Iterable[PointEvent] = (),
        tier_events: Iterable[TierEvent] = (),
        artist_ids: Iterable[int] = (),
        previous_update_dates: Iterable[tuple[int, date | None]] = (),
    ) -> None:
        point_ids = tuple(event.id for event in point_events)
        tier_ids = tuple(event.id for event in tier_events)
        ids = tuple(dict.fromkeys(int(value) for value in artist_ids))
        if point_ids or tier_ids:
            self._actions.append(_Action(point_ids, tier_ids, ids, tuple(previous_update_dates)))

    def _last_action_index(self, artist_id: int | None) -> int | None:
        for index in range(len(self._actions) - 1, -1, -1):
            if artist_id is None or artist_id in self._actions[index].artist_ids:
                return index
        return None

    @staticmethod
    def _sentiment_delta(sentiment: str | bool) -> tuple[int, str]:
        if sentiment is True:
            return 1, "Good"
        if sentiment is False:
            return -1, "Bad"
        normalized = str(sentiment).strip().casefold()
        if normalized in {"good", "positive", "+", "+1"}:
            return 1, "Good"
        if normalized in {"bad", "negative", "-", "-1"}:
            return -1, "Bad"
        raise RatingError("sentiment must be good/+1 or bad/-1")

    @staticmethod
    def _suggestion_identity(suggestion: RuleSuggestion) -> tuple[int, str, bool, int]:
        return (
            suggestion.artist_id,
            suggestion.rule_key,
            suggestion.is_reversal,
            suggestion.delta,
        )
