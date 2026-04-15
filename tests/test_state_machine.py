# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from sdr_ilha_ar.state_machine import assert_transition, can_transition


def test_valid_new_to_quoted():
    assert can_transition("new", "quoted") is True


def test_invalid_quoted_to_new():
    assert can_transition("quoted", "new") is False


def test_awaiting_slot_to_scheduled():
    assert_transition("awaiting_slot", "scheduled")


def test_invalid_transition_raises():
    import pytest

    with pytest.raises(ValueError):
        assert_transition("quoted", "new")
