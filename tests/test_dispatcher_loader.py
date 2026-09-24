from __future__ import annotations

import pytest

from multicloud_ai_agent_gateway.core.dispatcher import load_dispatcher


def test_returns_none_for_empty_spec() -> None:
    assert load_dispatcher(None) is None
    assert load_dispatcher("") is None


def test_loads_callable_from_module_path() -> None:
    fn = load_dispatcher("json:dumps")
    assert callable(fn)
    assert fn({"k": 1}) == '{"k": 1}'


def test_rejects_spec_without_colon() -> None:
    with pytest.raises(ValueError, match="module:attr"):
        load_dispatcher("json")


def test_raises_when_attribute_missing() -> None:
    with pytest.raises(AttributeError):
        load_dispatcher("json:no_such_attr_xyz")


def test_raises_when_attribute_is_not_callable() -> None:
    with pytest.raises(TypeError):
        load_dispatcher("sys:version")
