"""Data-only contracts for isolated visualization extensions."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from nexus.contracts.base import StrictContract, CommandDigest
from nexus.contracts.extensions import canonical, digest_value

ExtensionId = Annotated[StrictStr, Field(pattern=r'^user\.[a-z][a-z0-9_]{1,47}$')]
Number = StrictInt | StrictFloat
RUNTIME = 'visualization-worker-v1'


class UIExtensionSource(StrictContract):
    plugin_id: ExtensionId
    version: StrictStr = Field(pattern=r'^\d{1,4}\.\d{1,4}\.\d{1,4}$')
    name: StrictStr = Field(min_length=1, max_length=80)
    source: StrictStr = Field(min_length=1, max_length=20000)
    base_version_id: UUID | None = None

    @model_validator(mode='after')
    def bounded(self):
        if '\x00' in self.source or len(self.source.encode()) > 20000:
            raise ValueError('source must be at most 20000 UTF-8 bytes without NUL')
        return self


class UIExtensionInspectArguments(StrictContract):
    pass


class UIExtensionInspectProposal(StrictContract):
    intent: Literal['inspect_ui_extension']
    arguments: UIExtensionInspectArguments


class UIExtensionSubmitProposal(StrictContract):
    intent: Literal['submit_ui_extension']
    arguments: UIExtensionSource


class UIExtensionPolicy(StrictContract):
    expected_revision: StrictInt = Field(ge=0)
    enabled: StrictBool
    client_id: UUID
    plugin_id: ExtensionId = 'user.gesture_control'
    profile: Literal['gesture_navigation_v1'] = 'gesture_navigation_v1'


class UIClient(StrictContract):
    client_id: UUID
    runtime: Literal['visualization-worker-v1'] = RUNTIME
    platform: Literal['web', 'windows']


class UIExtensionDecision(StrictContract):
    action: Literal['enable', 'disable']
    expected_revision: StrictInt = Field(ge=1)
    expected_deployment_revision: StrictInt = Field(ge=1)
    version_id: UUID
    bundle_digest: CommandDigest


class UICommand(StrictContract):
    op: Literal['rotate', 'zoom', 'select', 'reset']
    dx: Number | None = None
    dy: Number | None = None
    delta: Number | None = None
    x: Number | None = None
    y: Number | None = None

    @model_validator(mode='after')
    def fields(self):
        expected = {'rotate': {'dx', 'dy'}, 'zoom': {'delta'}, 'select': {'x', 'y'}, 'reset': set()}[self.op]
        values = self.model_dump(exclude_none=True)
        if set(values) != expected | {'op'}:
            raise ValueError('command fields do not match operation')
        for key in expected:
            value = values[key]
            low, high = (0, 1) if key in {'x', 'y'} else (-200, 200)
            if not low <= value <= high:
                raise ValueError('command value outside host contract')
        return self


class UITestReport(StrictContract):
    lease_id: UUID
    client_id: UUID
    bundle_digest: CommandDigest
    outputs: list[list[list[UICommand]]] = Field(default_factory=list, max_length=16)
    error: StrictStr = Field(default='', max_length=500)

    @model_validator(mode='after')
    def bounded(self):
        if len(canonical(self.model_dump(mode='json')).encode()) > 24000:
            raise ValueError('test evidence too large')
        if any(len(case) > 16 or any(len(frame) > 4 for frame in case) for case in self.outputs):
            raise ValueError('test evidence shape too large')
        return self


class UIRuntimeReport(StrictContract):
    client_id: UUID
    version_id: UUID
    deployment_revision: StrictInt = Field(ge=1)
    bundle_digest: CommandDigest
    status: Literal['ready', 'failed']
    error: StrictStr = Field(default='', max_length=500)


def gesture_suite():
    """Host-owned behavior checks. Candidate code cannot change these assertions."""
    def frame(ms, *hands):
        return {'time_ms': ms, 'hands': list(hands)}
    def hand(x=.4, y=.5, pinch=True, opened=False):
        return {'x': x, 'y': y, 'pinch': pinch, 'open': opened, 'landmarks': []}
    empty = []
    return [
        {'name': 'idle', 'frames': [frame(0), frame(40), frame(80, hand(pinch=False))], 'expect': [empty, empty, empty]},
        {'name': 'rotate', 'frames': [frame(0, hand()), frame(40, hand(.5, .55)), frame(80)],
         'expect': [empty, [{'op': 'rotate', 'dx': [1, 200], 'dy': [1, 200]}], empty]},
        {'name': 'zoom', 'frames': [frame(0, hand(.3), hand(.7)), frame(40, hand(.2), hand(.8)), frame(80)],
         'expect': [empty, [{'op': 'zoom', 'delta': [-200, -1]}], empty]},
        {'name': 'select', 'frames': [frame(0, hand()), frame(80, hand(pinch=False))],
         'expect': [empty, [{'op': 'select', 'x': [.35, .45], 'y': [.45, .55]}]]},
        {'name': 'reset', 'frames': [frame(0, hand(pinch=False, opened=True), hand(.7, pinch=False, opened=True)),
                                     frame(40, hand(pinch=False, opened=True), hand(.7, pinch=False, opened=True))],
         'expect': [[{'op': 'reset'}], empty]},
    ]


def build_bundle(value: UIExtensionSource):
    # Packaging needs no candidate execution, npm scripts, filesystem paths or imports.
    bundle = {'format': 1, 'runtime': RUNTIME, 'plugin_id': value.plugin_id,
              'version': value.version, 'name': value.name, 'source': value.source,
              'suite_digest': digest_value(gesture_suite())}
    return bundle, digest_value(bundle)


def check_outputs(outputs):
    suite = gesture_suite()
    if len(outputs) != len(suite):
        return False
    for case, actual in zip(suite, outputs):
        if len(actual) != len(case['expect']):
            return False
        for expected, commands in zip(case['expect'], actual):
            if len(expected) != len(commands):
                return False
            for rule, command in zip(expected, commands):
                data = command.model_dump(exclude_none=True)
                if set(rule) != set(data) or rule['op'] != data['op']:
                    return False
                if any(not bounds[0] <= data[key] <= bounds[1] for key, bounds in rule.items() if key != 'op'):
                    return False
    return True
