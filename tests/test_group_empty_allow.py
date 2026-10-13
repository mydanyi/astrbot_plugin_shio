"""Red tests for private and group allowlist admission (stdlib unittest, no source edits)."""
import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("sys001_under_test", ROOT / "core" / "sys001.py")
sys001 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = sys001
_SPEC.loader.exec_module(sys001)


def make_snapshot(**changes):
    base = dict(platform_id="aiocqhttp", scope="fixture-qq:GroupMessage:10001",
                account_id="fixture-qq", sender_id="101", sender_name="member",
                self_id="42", message_id="event",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                message_text="hello", is_private=False, is_master=False,
                at_self=True, reply_to_self=False, origin="pending", group_id="10001")
    base.update(changes)
    return sys001.TurnSnapshot(**base)


def decide(snapshot, private=(), blocked=(), groups=(), blocked_groups=()):
    return sys001.admit_ingress(snapshot, private_allowed_sender_ids=frozenset(private),
                                blocked_sender_ids=frozenset(blocked),
                                group_allowed_scopes=frozenset(groups),
                                group_blocked_scopes=frozenset(blocked_groups))


class EmptyGroupWhitelistTests(unittest.TestCase):
    def test_two_groups_allowed_when_group_whitelist_empty(self):
        for group_id in ("10001", "20002"):
            with self.subTest(group_id=group_id):
                snap = make_snapshot(group_id=group_id, scope="fixture-qq:GroupMessage:" + group_id)
                self.assertTrue(decide(snap).allowed)

    def test_group_id_whitelist_match_allows(self):
        snap = make_snapshot()
        self.assertTrue(decide(snap, groups=("10001",)).allowed)

    def test_full_scope_whitelist_match_allows(self):
        snap = make_snapshot()
        self.assertTrue(decide(snap, groups=("fixture-qq:GroupMessage:10001",)).allowed)

    def test_group_whitelist_mismatch_denies(self):
        snap = make_snapshot()
        self.assertFalse(decide(snap, groups=("99999", "fixture-qq:GroupMessage:99999")).allowed)

    def test_empty_whitelist_still_denies_group_id_blocklist(self):
        snap = make_snapshot()
        self.assertFalse(decide(snap, blocked_groups=("10001",)).allowed)

    def test_empty_whitelist_still_denies_scope_blocklist(self):
        snap = make_snapshot()
        self.assertFalse(decide(snap, blocked_groups=("fixture-qq:GroupMessage:10001",)).allowed)

    def test_empty_whitelist_still_denies_sender_blocklist(self):
        snap = make_snapshot()
        self.assertFalse(decide(snap, blocked=("101",)).allowed)

    def test_identity_missing_or_self_speech_denies(self):
        cases = (make_snapshot(sender_id=""), make_snapshot(self_id=""),
                 make_snapshot(sender_id="42"))
        for snap in cases:
            with self.subTest(sender_id=snap.sender_id, self_id=snap.self_id):
                self.assertFalse(decide(snap).allowed)

    def test_empty_private_whitelist_admits_two_users(self):
        for sender_id in ("101", "102"):
            with self.subTest(sender_id=sender_id):
                snap = make_snapshot(sender_id=sender_id, is_private=True, group_id="",
                                     scope="fixture-qq:FriendMessage:" + sender_id)
                decision = decide(snap)
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reason, "private_sender_allowed")

    def test_empty_private_whitelist_but_blocked_denies(self):
        snap = make_snapshot(is_private=True, group_id="", scope="fixture-qq:FriendMessage:101")
        self.assertFalse(decide(snap, blocked=("101",)).allowed)

    def test_nonempty_private_whitelist_unlisted_denies(self):
        snap = make_snapshot(sender_id="102", is_private=True, group_id="",
                             scope="fixture-qq:FriendMessage:102")
        self.assertFalse(decide(snap, private=("101",)).allowed)

    def test_private_missing_sender_or_self_or_self_speaker_denies(self):
        cases = (
            make_snapshot(sender_id="", is_private=True, group_id="",
                          scope="fixture-qq:FriendMessage:101"),
            make_snapshot(self_id="", is_private=True, group_id="",
                          scope="fixture-qq:FriendMessage:101"),
            make_snapshot(sender_id="42", is_private=True, group_id="",
                          scope="fixture-qq:FriendMessage:42"),
        )
        for snap in cases:
            with self.subTest(sender_id=snap.sender_id, self_id=snap.self_id):
                self.assertFalse(decide(snap).allowed)

    def test_private_listed_sender_allows(self):
        snap = make_snapshot(is_private=True, group_id="", scope="fixture-qq:FriendMessage:101")
        self.assertTrue(decide(snap, private=("101",)).allowed)

    def test_private_listed_but_blocked_sender_denies(self):
        snap = make_snapshot(is_private=True, group_id="", scope="fixture-qq:FriendMessage:101")
        self.assertFalse(decide(snap, private=("101",), blocked=("101",)).allowed)

    def test_master_allowed_despite_blocklists(self):
        group_snap = make_snapshot(is_master=True)
        private_snap = make_snapshot(is_master=True, is_private=True, group_id="",
                                     scope="fixture-qq:FriendMessage:101")
        for label, snap in (("group", group_snap), ("private", private_snap)):
            with self.subTest(where=label):
                self.assertTrue(decide(snap, blocked=("101",),
                                       blocked_groups=("10001", "fixture-qq:GroupMessage:10001")).allowed)


if __name__ == "__main__":
    unittest.main()
