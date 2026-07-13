"""Governor sync policy: approval gate + dry-run forcing (PRD §8).

Needs the stack installed (pydantic/sqlalchemy) but NOT a database — check_action
is pure and Governor(session=None) never touches the DB for it.
"""

from switchboard.config import get_settings
from switchboard.governor.governor import Governor
from switchboard.interfaces import PlanItemView


def _gov():
    return Governor(session=None, settings=get_settings())


def _item(**kw):
    base = dict(assigned_agent="production", action_type="create_asana_task", brand="hotcars")
    base.update(kw)
    return PlanItemView(**base)


def test_live_action_on_unapproved_item_refused():
    d = _gov().check_action(_item(status="proposed", dry_run=False))
    assert d.allowed is False
    assert "approval_gate" in d.violations


def test_approved_live_item_allowed_live():
    d = _gov().check_action(_item(status="approved", dry_run=False))
    assert d.allowed is True and d.dry_run is False


def test_approved_dryrun_item_stays_dryrun():
    d = _gov().check_action(_item(status="approved", dry_run=True))
    assert d.allowed is True and d.dry_run is True


def test_proposed_dryrun_item_allowed_as_dryrun():
    d = _gov().check_action(_item(status="proposed", dry_run=True))
    assert d.allowed is True and d.dry_run is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
