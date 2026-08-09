from __future__ import annotations

import inspect
import types
from collections.abc import Iterator
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

import pytest

from problem_locator.contracts import ports

from tests.deterministic.contracts import fakes


# The four private classes below are capabilities produced by public fakes.  They
# are listed explicitly so their frozen lease/sink/session surfaces receive the
# same static signature checks as directly constructible public fakes.
PORT_FAKE_TYPES: dict[type, type] = {
    ports.AppendOnlyByteSink: fakes._InMemoryAppendOnlyByteSink,
    ports.ApplicationCommandPort: fakes.RecordingApplicationCommand,
    ports.ApplicationQueryPort: fakes.StubApplicationQuery,
    ports.AssetCatalogPort: fakes.FakeAssetCatalog,
    ports.AttachmentUploadGuard: fakes.InMemoryAttachmentUploadGuard,
    ports.AttachmentUploadLease: fakes._AttachmentLease,
    ports.BinaryStream: fakes.InMemoryBinaryStream,
    ports.CancellationSignal: fakes.InMemoryCancellationSignal,
    ports.Clock: fakes.FakeClock,
    ports.ContextSnapshotProjector: fakes.PureContextSnapshotProjector,
    ports.Coordinator: fakes.ScriptedCoordinator,
    ports.Dispatcher: fakes.RecordingDispatcher,
    ports.ExecutionRecordStore: fakes.InMemoryExecutionRecordStore,
    ports.IdGenerator: fakes.DeterministicIdGenerator,
    ports.JobControlPort: fakes.StubJobControl,
    ports.LogparseBrokerFactory: fakes.FakeLogparseBrokerFactory,
    ports.LogparseBrokerSession: fakes._FakeLogparseBrokerSession,
    ports.PublicationCommitGuard: fakes.InMemoryPublicationCommitGuard,
    ports.PublicationCommitLease: fakes._PublicationLease,
    ports.ResourceStore: fakes.InMemoryResourceStore,
    ports.Runtime: fakes.ScriptedRuntime,
    ports.StateAdminPort: fakes.StubStateAdmin,
    ports.StateChangeNotifier: fakes.InMemoryStateChangeNotifier,
    ports.StateRepository: fakes.InMemoryStateRepository,
}


def _declared_protocol_members(
    protocol: type,
) -> Iterator[tuple[str, object]]:
    """Yield only members declared by this Protocol, including context hooks."""

    for name, member in protocol.__dict__.items():
        # typing.Protocol injects a non-contractual constructor into every
        # subclass; ports.py does not declare constructors on its interfaces.
        if name == "__init__":
            continue
        if isinstance(member, property) or inspect.isfunction(member):
            yield name, member


def _callable(member: object) -> Any:
    if isinstance(member, property):
        assert member.fget is not None
        return member.fget
    assert inspect.isfunction(member)
    return member


def _default_key(value: object) -> tuple[type, object]:
    if value is inspect.Signature.empty:
        return (type(inspect.Signature.empty), "missing")
    try:
        hash(value)
    except TypeError:
        return (type(value), repr(value))
    return (type(value), value)


def _normalise_annotation(annotation: object) -> object:
    """Collapse spelling-only differences while retaining type information."""

    if annotation is None:
        return type(None)
    origin = get_origin(annotation)
    if origin is Annotated:
        return _normalise_annotation(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        return (
            "union",
            frozenset(_normalise_annotation(item) for item in get_args(annotation)),
        )
    if origin is not None:
        return (
            origin,
            tuple(_normalise_annotation(item) for item in get_args(annotation)),
        )
    return annotation


def _resolved_return(function: Any) -> object:
    hints = get_type_hints(function, include_extras=False)
    return hints.get("return", inspect.Signature.empty)


SIGNATURE_CASES = [
    (protocol, fake_type, member_name, protocol_member)
    for protocol, fake_type in PORT_FAKE_TYPES.items()
    for member_name, protocol_member in _declared_protocol_members(protocol)
]


def test_signature_mapping_covers_every_public_protocol() -> None:
    public_protocols = {getattr(ports, name) for name in ports.__all__}
    assert set(PORT_FAKE_TYPES) == public_protocols
    assert all(inspect.isclass(fake_type) for fake_type in PORT_FAKE_TYPES.values())


@pytest.mark.parametrize(
    ("protocol", "fake_type", "member_name", "protocol_member"),
    SIGNATURE_CASES,
    ids=[
        f"{protocol.__name__}->{fake_type.__name__}.{member_name}"
        for protocol, fake_type, member_name, _ in SIGNATURE_CASES
    ],
)
def test_fake_member_has_the_frozen_static_signature(
    protocol: type,
    fake_type: type,
    member_name: str,
    protocol_member: object,
) -> None:
    fake_member = inspect.getattr_static(fake_type, member_name)

    assert isinstance(fake_member, property) == isinstance(protocol_member, property), (
        f"{fake_type.__name__}.{member_name} must preserve the Protocol member kind"
    )

    protocol_callable = _callable(protocol_member)
    fake_callable = _callable(fake_member)
    assert inspect.iscoroutinefunction(fake_callable) == inspect.iscoroutinefunction(
        protocol_callable
    )

    expected = inspect.signature(protocol_callable)
    actual = inspect.signature(fake_callable)
    expected_parameters = list(expected.parameters.values())
    actual_parameters = list(actual.parameters.values())

    assert [parameter.name for parameter in actual_parameters] == [
        parameter.name for parameter in expected_parameters
    ], f"{protocol.__name__}.{member_name} parameter names changed"
    assert [parameter.kind for parameter in actual_parameters] == [
        parameter.kind for parameter in expected_parameters
    ], f"{protocol.__name__}.{member_name} parameter kinds changed"
    assert [_default_key(parameter.default) for parameter in actual_parameters] == [
        _default_key(parameter.default) for parameter in expected_parameters
    ], f"{protocol.__name__}.{member_name} parameter defaults changed"

    expected_hints = get_type_hints(protocol_callable, include_extras=False)
    actual_hints = get_type_hints(fake_callable, include_extras=False)
    for parameter in expected_parameters:
        if parameter.name == "self":
            continue
        expected_annotation = expected_hints.get(
            parameter.name,
            inspect.Signature.empty,
        )
        actual_annotation = actual_hints.get(
            parameter.name,
            inspect.Signature.empty,
        )
        assert expected_annotation is not inspect.Signature.empty
        assert actual_annotation is not inspect.Signature.empty
        assert _normalise_annotation(actual_annotation) == _normalise_annotation(
            expected_annotation
        ), (
            f"{fake_type.__name__}.{member_name} parameter {parameter.name!r} "
            f"annotation {actual_annotation!r} is not equivalent to "
            f"{protocol.__name__}'s {expected_annotation!r}"
        )

    expected_return = _resolved_return(protocol_callable)
    actual_return = _resolved_return(fake_callable)
    assert expected_return is not inspect.Signature.empty
    assert actual_return is not inspect.Signature.empty
    assert _normalise_annotation(actual_return) == _normalise_annotation(
        expected_return
    ), (
        f"{fake_type.__name__}.{member_name} return annotation {actual_return!r} "
        f"is not equivalent to {protocol.__name__}'s {expected_return!r}"
    )
