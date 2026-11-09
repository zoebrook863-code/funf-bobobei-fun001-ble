"""Live policy after the observed Windows disconnect failure.

Legacy bench entries fail closed. Normal local control may use the verified
envelope under the owner-accepted residual-risk rules. The phase-2 one-shot
gate stays closed.
"""

NONZERO_BLOCK_REASON = 'motion_continued_after_windows_session_close'


def nonzero_bench_allowed():
    # Re-enable only after a reviewed mitigation and separate physical acceptance.
    return False


def live_nonzero_allowed():
    # Live commands require a local owner grant or standing permission.
    # Software watchdogs depend on a working process and BLE link.
    # Hardware disconnect-stop limitations are documented in docs/SAFETY.md.
    return True


def phase2_disconnect_allowed():
    # One-shot hardware diagnostic gate. Enable only after a reviewed
    # test plan and the owner's separate on-site confirmation.
    return False


def blocked_nonzero_result():
    return {'status': 'blocked_by_disconnect_safety',
            'reason': NONZERO_BLOCK_REASON, 'nonzero_attempts': 0,
            'device_state': 'unknown'}
