"""The build queue is a work list, not a transcript of every suggestion ever made."""
import operator2
import bootstrap as B


def _reset():
    B.BUILD_QUEUE[:] = []


def test_identical_commands_are_queued_once():
    """The architect re-escalates whenever the base looks stalled, so it re-issues the same
    handful of commands every pass. The live queue reached 256 entries that were mostly
    `connect_mine iron-ore` and `repair power`, over and over."""
    _reset()
    cmd = [{"cmd": "connect_mine", "ore": "iron-ore"}]
    a1, _ = operator2.validate_commands(cmd)
    a2, r2 = operator2.validate_commands(cmd)
    assert a1 == ["connect_mine iron-ore"]
    assert a2 == [] and any("already queued" in r for r in r2)
    assert len(B.BUILD_QUEUE) == 1
    _reset()


def test_a_different_command_still_gets_through():
    _reset()
    operator2.validate_commands([{"cmd": "connect_mine", "ore": "iron-ore"}])
    acc, _ = operator2.validate_commands([{"cmd": "connect_mine", "ore": "copper-ore"}])
    assert acc == ["connect_mine copper-ore"]
    assert len(B.BUILD_QUEUE) == 2
    _reset()


def test_the_queue_is_capped():
    """A queue that grows without bound is a log. Past the cap the right answer is to stop
    accepting: that much backlog means the front of it is not running."""
    _reset()
    for i in range(operator2.QUEUE_CAP + 5):
        operator2.validate_commands([{"cmd": "plan_note", "text": "note %d" % i}])
    assert len(B.BUILD_QUEUE) == operator2.QUEUE_CAP
    _reset()


def test_the_cap_says_why_it_refused():
    _reset()
    for i in range(operator2.QUEUE_CAP):
        operator2.validate_commands([{"cmd": "plan_note", "text": "n%d" % i}])
    _, rej = operator2.validate_commands([{"cmd": "plan_note", "text": "one more"}])
    assert any("not draining" in r for r in rej)
    _reset()


def test_a_drained_command_can_be_queued_again():
    """Dedupe is against what is PENDING, not against history - a job that ran and is needed
    again must be requeueable."""
    _reset()
    cmd = [{"cmd": "repair", "target": "power"}]
    operator2.validate_commands(cmd)
    B.BUILD_QUEUE.pop(0)
    acc, _ = operator2.validate_commands(cmd)
    assert acc == ["repair power"]
    _reset()
