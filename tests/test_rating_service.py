from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from collection_manager.constants import (
    CollectionKind,
    HeavyStatus,
    PointEventKind,
    RuleKind,
    Tier,
)
from collection_manager.models import Base, PointEvent, RuleEffect, TierEvent
from collection_manager.rating_service import (
    EventNotReversibleError,
    NoTierShiftError,
    RatingError,
    RatingService,
    StaleRuleSuggestionError,
)
from collection_manager.repository import ArtistRepository


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False, autoflush=False) as value:
        yield value


def test_log_update_requires_date_and_records_exact_vibe_delta(session: Session) -> None:
    artist = ArtistRepository(session).create("Vibes", last_updated=date(2025, 1, 1))
    service = RatingService(session)

    good = service.log_update(artist.id, date(2026, 4, 3), "good", "Strong new course")
    bad = service.log_update(artist.id, date(2026, 5, 4), False)

    assert (good.delta, good.old_points, good.new_points) == (1, 0, 1)
    assert (bad.delta, bad.old_points, bad.new_points) == (-1, 1, 0)
    assert good.kind == PointEventKind.UPDATE_VIBE.value
    assert artist.points == 0
    assert artist.last_updated == date(2026, 5, 4)

    with pytest.raises(RatingError, match="date"):
        service.log_update(artist.id, None, "good")  # type: ignore[arg-type]
    with pytest.raises(RatingError, match="sentiment"):
        service.log_update(artist.id, date(2026, 5, 5), "mixed")


def test_undo_vibe_is_auditable_and_restores_update_date(session: Session) -> None:
    original_date = date(2025, 2, 1)
    artist = ArtistRepository(session).create("Undo", last_updated=original_date)
    service = RatingService(session)
    event = service.log_update(artist.id, date(2026, 2, 1), "good")

    reversal = service.undo_last(artist.id)

    assert isinstance(reversal, PointEvent)
    assert reversal.kind == PointEventKind.REVERSAL.value
    assert reversal.delta == -1
    assert artist.points == 0
    assert artist.last_updated == original_date
    assert event.reversed_event_id == reversal.id
    with pytest.raises(EventNotReversibleError, match="already"):
        service.reverse_event(event.id)


def test_direct_reversal_rejects_older_event_with_same_resulting_balance(
    session: Session,
) -> None:
    artist = ArtistRepository(session).create("Latest only")
    service = RatingService(session)
    first = service.adjust_points(artist.id, 1, "First +1")
    service.adjust_points(artist.id, -1, "Back to zero")
    latest = service.adjust_points(artist.id, 1, "Second +1")
    tied_timestamp = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    for event_record in session.scalars(select(PointEvent)).all():
        event_record.created_at = tied_timestamp
    session.flush()

    assert first.new_points == latest.new_points == artist.points == 1
    with pytest.raises(EventNotReversibleError, match="actual latest"):
        service.reverse_event(first.id)

    reversal = service.reverse_event(latest.id)
    assert isinstance(reversal, PointEvent)
    assert artist.points == 0


def test_direct_tier_reset_reversal_restores_tier_and_points_atomically(
    session: Session,
) -> None:
    artist = ArtistRepository(session).create("Atomic reset", tier=Tier.WORTH_REVISITING, points=2)
    service = RatingService(session)
    service.adjust_points(artist.id, 1, "Threshold")
    original_tier_event = service.approve_tier_shift(artist.id)
    reset = session.scalar(
        select(PointEvent)
        .where(PointEvent.kind == PointEventKind.TIER_RESET.value)
        .order_by(PointEvent.id.desc())
    )
    assert reset is not None

    reversal = service.reverse_event(reset.id, "Reverse promotion")

    assert isinstance(reversal, TierEvent)
    assert artist.tier_value is Tier.WORTH_REVISITING
    assert artist.points == 3
    assert original_tier_event.reversed_event_id == reversal.id
    assert reset.reversed_event_id is not None


def test_unpaired_tier_reset_cannot_be_reversed_as_points_only(session: Session) -> None:
    artist = ArtistRepository(session).create("Unpaired reset", points=2)
    reset = PointEvent(
        artist_id=artist.id,
        kind=PointEventKind.TIER_RESET.value,
        delta=-2,
        reason="Synthetic unpaired reset",
        old_points=2,
        new_points=0,
    )
    artist.points = 0
    session.add(reset)
    session.flush()

    with pytest.raises(EventNotReversibleError, match="paired tier event"):
        RatingService(session).reverse_event(reset.id)
    assert artist.points == 0
    assert reset.reversed_event_id is None


def test_session_only_undo_never_falls_back_to_persisted_history(session: Session) -> None:
    artist = ArtistRepository(session).create("Session boundary")
    writer = RatingService(session)
    writer.adjust_points(artist.id, 1, "Persisted action")
    restarted_service = RatingService(session)

    assert restarted_service.undo_last_session_action(artist.id) is None
    assert artist.points == 1

    fallback_reversal = restarted_service.undo_last(artist.id)
    assert isinstance(fallback_reversal, PointEvent)
    assert artist.points == 0


def test_rule_evaluation_apply_dedupe_and_condition_reversals(session: Session) -> None:
    repository = ArtistRepository(session)
    artist = repository.create(
        "Rule Artist",
        last_updated=date(2023, 6, 15),
        heavy_status=HeavyStatus.YES,
        is_compressed=False,
        tags=["one", "two", "three", "four", "five", "FIVE"],
    )
    service = RatingService(session)

    suggestions = service.evaluate_rules(date(2026, 6, 14))
    assert [(item.rule_kind, item.delta) for item in suggestions] == [
        (RuleKind.INACTIVITY, -1),
        (RuleKind.INACTIVITY, -1),
        (RuleKind.HEAVY, -1),
        (RuleKind.DIVERSITY, 1),
    ]
    assert artist.points == 0  # evaluation is non-mutating

    events = service.apply_rule_suggestions(suggestions, date(2026, 6, 14))
    assert len(events) == 4
    assert artist.points == -2
    assert artist.date_evaluated == date(2026, 6, 14)
    assert service.evaluate_rules(date(2026, 6, 14)) == []
    assert session.scalar(select(RuleEffect).where(RuleEffect.active.is_(True))) is not None

    repository.update(
        artist.id,
        last_updated=date(2026, 6, 14),
        is_compressed=True,
        tags=["one", "two"],
    )
    reversals = service.evaluate_rules(date(2026, 6, 14))
    assert len(reversals) == 4
    assert all(item.is_reversal for item in reversals)
    assert sorted(item.delta for item in reversals) == [-1, 1, 1, 1]

    service.apply_rule_suggestions(reversals, date(2026, 6, 14))
    assert artist.points == 0
    assert service.evaluate_rules(date(2026, 6, 14)) == []
    assert session.scalars(select(RuleEffect).where(RuleEffect.active.is_(True))).all() == []


def test_rule_evaluation_can_be_scoped_to_one_collection(session: Session) -> None:
    repository = ArtistRepository(session)
    video = repository.create(
        "Shared Heavy",
        collection_kind=CollectionKind.VIDEOS,
        heavy_status=HeavyStatus.YES,
    )
    image = repository.create(
        "Shared Heavy",
        collection_kind=CollectionKind.IMAGES,
        heavy_status=HeavyStatus.YES,
    )
    service = RatingService(session)

    video_suggestions = service.evaluate_rules(
        date(2026, 1, 1), collection_kind=CollectionKind.VIDEOS
    )
    image_suggestions = service.evaluate_rules(
        date(2026, 1, 1), collection_kind=CollectionKind.IMAGES
    )

    assert {suggestion.artist_id for suggestion in video_suggestions} == {video.id}
    assert {suggestion.artist_id for suggestion in image_suggestions} == {image.id}


def test_stale_or_duplicate_rule_selection_is_rejected(session: Session) -> None:
    repository = ArtistRepository(session)
    artist = repository.create("Heavy", heavy_status=HeavyStatus.YES)
    service = RatingService(session)
    suggestion = service.evaluate_rules(date(2026, 1, 1))[0]

    with pytest.raises(RatingError, match="more than once"):
        service.apply_rule_suggestions([suggestion, suggestion], date(2026, 1, 1))

    repository.update(artist.id, is_compressed=True)
    with pytest.raises(StaleRuleSuggestionError, match="no longer current"):
        service.apply_rule_suggestions([suggestion], date(2026, 1, 1))
    assert artist.points == 0


def test_rule_batch_threshold_is_deferred_to_attention(session: Session) -> None:
    repository = ArtistRepository(session)
    artist = repository.create(
        "Diverse",
        tier=Tier.WORTH_REVISITING,
        points=2,
        tags=["1", "2", "3", "4", "5"],
    )
    service = RatingService(session)

    event = service.apply_rule_suggestions(service.evaluate_rules(date(2026, 1, 1)))[0]

    assert artist.points == 3
    assert event.pending_tier_shift is True
    assert repository.list(attention_only=True) == [artist]


@pytest.mark.parametrize(
    ("tier", "points", "destination"),
    [
        (Tier.BORING, 3, Tier.FELL_OFF),
        (Tier.FELL_OFF, 3, Tier.WORTH_REVISITING),
        (Tier.WORTH_REVISITING, 3, Tier.UNSAVABLE_BANGERS),
        (Tier.UNSAVABLE_BANGERS, 3, Tier.BANGERS),
        (Tier.BANGERS, 3, Tier.CREAM),
        (Tier.CREAM, -3, Tier.BANGERS),
        (Tier.UNSAVABLE_BANGERS, -3, Tier.WORTH_REVISITING),
        (Tier.WORTH_REVISITING, -3, Tier.FELL_OFF),
        (Tier.FELL_OFF, -3, Tier.BORING),
        (Tier.BANGERS, -3, Tier.FELL_OFF),
    ],
)
def test_legal_tier_destinations(
    session: Session, tier: Tier, points: int, destination: Tier
) -> None:
    artist = ArtistRepository(session).create(f"{tier}-{points}", tier=tier, points=points)
    proposal = RatingService(session).propose_tier_shift(artist.id)

    assert proposal is not None
    assert proposal.old_tier is tier
    assert proposal.new_tier is destination


def test_tier_boundaries_have_no_proposal(session: Session) -> None:
    repository = ArtistRepository(session)
    cream = repository.create("Cream", tier=Tier.CREAM, points=3)
    boring = repository.create("Boring", tier=Tier.BORING, points=-3)
    service = RatingService(session)

    assert service.propose_tier_shift(cream.id) is None
    assert service.propose_tier_shift(boring.id) is None
    with pytest.raises(NoTierShiftError):
        service.defer_tier_shift(cream.id)


def test_defer_approve_and_undo_tier_shift(session: Session) -> None:
    repository = ArtistRepository(session)
    artist = repository.create("Promote", tier=Tier.WORTH_REVISITING, points=2)
    service = RatingService(session)
    trigger = service.adjust_points(artist.id, 1, "Reached threshold")

    service.defer_tier_shift(artist.id)
    assert trigger.pending_tier_shift is True
    assert repository.list(attention_only=True) == [artist]

    tier_event = service.approve_tier_shift(artist.id)
    assert artist.tier_value is Tier.UNSAVABLE_BANGERS
    assert artist.points == 0
    assert trigger.pending_tier_shift is False
    reset = session.scalar(
        select(PointEvent)
        .where(PointEvent.kind == PointEventKind.TIER_RESET.value)
        .order_by(PointEvent.id.desc())
    )
    assert reset is not None and (reset.old_points, reset.new_points) == (3, 0)
    assert tier_event.triggering_point_event_id == trigger.id

    reversal = service.undo_last(artist.id)
    assert isinstance(reversal, TierEvent)
    assert artist.tier_value is Tier.WORTH_REVISITING
    assert artist.points == 3
    assert tier_event.reversed_event_id == reversal.id
    assert repository.list(attention_only=True) == [artist]


def test_later_point_change_clears_a_stale_attention_marker(session: Session) -> None:
    repository = ArtistRepository(session)
    artist = repository.create("Stale attention", tier=Tier.WORTH_REVISITING, points=2)
    service = RatingService(session)
    service.adjust_points(artist.id, 1, "Threshold")
    service.defer_tier_shift(artist.id)

    service.adjust_points(artist.id, -1, "Balance changed")

    assert artist.points == 2
    assert repository.list(attention_only=True) == []


def test_manual_override_requires_reason_resets_and_is_undoable(session: Session) -> None:
    artist = ArtistRepository(session).create("Override", tier=Tier.BORING, points=2)
    service = RatingService(session)

    with pytest.raises(RatingError, match="reason"):
        service.manual_tier_override(artist.id, Tier.CREAM, "")

    event = service.manual_tier_override(artist.id, Tier.CREAM, "Curator decision")
    assert artist.tier_value is Tier.CREAM
    assert artist.points == 0
    assert event.points_before_reset == 2

    service.undo_last(artist.id)
    assert artist.tier_value is Tier.BORING
    assert artist.points == 2
    assert len(session.scalars(select(TierEvent)).all()) == 2
    assert len(session.scalars(select(PointEvent)).all()) == 2


def test_projected_proposal_allows_cancel_before_writing(session: Session) -> None:
    artist = ArtistRepository(session).create("Preview", tier=Tier.FELL_OFF, points=2)
    service = RatingService(session)

    proposal = service.propose_tier_shift(artist.id, points=artist.points + 1)

    assert proposal is not None and proposal.new_tier is Tier.WORTH_REVISITING
    assert artist.points == 2
    assert session.scalars(select(PointEvent)).all() == []
