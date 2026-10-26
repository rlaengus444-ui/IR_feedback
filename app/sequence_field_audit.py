"""Fail-closed pre-edit audit of every Sequences field saved by M1/S3.

Bindings copied from unchanged LegacySequenceParameterWorkspace.Shared.cs
CreateTextBindings/CreateToggleBindings and BjtMachineSequences policy.
Both audited profiles: bidirectional scanning -> TwoLane; OutputPurge cleaning.
No source loading, UI invocation, parameter write or automatic Load occurs here.
"""

from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
import re


class SequenceFieldAuditError(ValueError):
    pass


SOURCE_BINDINGS_SHA256 = '3214681922ea113ca404dfa7449bd7a9794fbf6a35422aa26b9d54cb3c518032'
FIELD_BINDINGS = {'checkBoxAdaptiveHeating': ('Printing', 'AdaptiveRecoatingSpeed', 'toggle'),
 'checkBoxCTHAuto': ('Printing', 'CarrierToHopperAuto', 'toggle'),
 'checkBoxInvertImage': ('Printing', 'InvertImage', 'toggle'),
 'checkBoxPass1CompactAndSpread': ('Printing', 'RecoatPass1CompactAndSpread', 'toggle'),
 'checkBoxPass1FeedPowder': ('Printing', 'RecoatPass1FeedPowder', 'toggle'),
 'checkBoxPass1Heater1': ('Printing', 'RecoatPass1UseHeater1', 'toggle'),
 'checkBoxPass1Heater2': ('Printing', 'RecoatPass1UseHeater2', 'toggle'),
 'checkBoxPass2CompactAndSpread': ('Printing', 'RecoatPass2CompactAndSpread', 'toggle'),
 'checkBoxPass2FeedPowder': ('Printing', 'RecoatPass2FeedPowder', 'toggle'),
 'checkBoxPass2Heater1': ('Printing', 'RecoatPass2UseHeater1', 'toggle'),
 'checkBoxPass2Heater2': ('Printing', 'RecoatPass2UseHeater2', 'toggle'),
 'checkBoxRandomizePixelShift': ('Printing', 'PrintRandomizePixelShift', 'toggle'),
 'checkBoxUseHopper': ('Printing', 'UseHopper', 'toggle'),
 'checkBoxUseSpitting': ('Printing', 'UseAutoSpitting', 'toggle'),
 'textBoxAutoCleaningInterval': ('Printing', 'AutoCleaningIntervalMins', 'text'),
 'textBoxAutoSpittingDrops': ('Printing', 'AutoSpittingDrops', 'text'),
 'textBoxBiDirCompensationMm': ('Printing', 'BiDirCompensationMm', 'text'),
 'textBoxCTHTrigger': ('Printing', 'CarrierToHopperTrigger', 'text'),
 'textBoxCleanEnd': ('PrintingMotion', 'Y_CleaningEndPosition', 'text'),
 'textBoxCleanStart': ('PrintingMotion', 'Y_CleaningStartPosition', 'text'),
 'textBoxCleanX': ('PrintingMotion', 'X_CleaningPosition', 'text'),
 'textBoxCleaningFrequencyLayers': ('Printing', 'CleaningIntervalLayers', 'text'),
 'textBoxCleaningSpeed': ('PrintingMotion', 'Y_CleaningVelocity', 'text'),
 'textBoxCompactingVelocity': ('Printing', 'F_CompactingSpeed', 'text'),
 'textBoxDesiredTemperature': ('Printing', 'FoundationTemperature', 'text'),
 'textBoxFeederEnd1': ('PrintingMotion', 'F_FeederPass1EndPosition', 'text'),
 'textBoxFeederEnd2': ('PrintingMotion', 'F_FeederPass2EndPosition', 'text'),
 'textBoxFeederRetractionDegrees': ('Printing', 'PowderFeederRetractionDegrees', 'text'),
 'textBoxFeederRpm': ('Printing', 'PowderFeederRpm', 'text'),
 'textBoxFeederStart1': ('PrintingMotion', 'F_FeederPass1StartPosition', 'text'),
 'textBoxFeederStart2': ('PrintingMotion', 'F_FeederPass2StartPosition', 'text'),
 'textBoxFeedingVelocity': ('Printing', 'F_FeedingSpeed', 'text'),
 'textBoxFoundationHeatingCount': ('Printing', 'FoundationHeatingCount', 'text'),
 'textBoxFoundationHeatingVelocity': ('Printing', 'FoundationHeatingSpeed', 'text'),
 'textBoxFoundationRecoatingCount': ('Printing', 'FoundationRecoatingCount', 'text'),
 'textBoxHeater1End1': ('PrintingMotion', 'F_Heater1Pass1EndPosition', 'text'),
 'textBoxHeater1End2': ('PrintingMotion', 'F_Heater1Pass2EndPosition', 'text'),
 'textBoxHeater1Start1': ('PrintingMotion', 'F_Heater1Pass1StartPosition', 'text'),
 'textBoxHeater1Start2': ('PrintingMotion', 'F_Heater1Pass2StartPosition', 'text'),
 'textBoxHeater2End1': ('PrintingMotion', 'F_Heater2Pass1EndPosition', 'text'),
 'textBoxHeater2End2': ('PrintingMotion', 'F_Heater2Pass2EndPosition', 'text'),
 'textBoxHeater2Start1': ('PrintingMotion', 'F_Heater2Pass1StartPosition', 'text'),
 'textBoxHeater2Start2': ('PrintingMotion', 'F_Heater2Pass2StartPosition', 'text'),
 'textBoxHeatingOutput': ('Printing', 'IRHeatingOutputPct', 'text'),
 'textBoxHopperOpenDuration': ('Printing', 'HopperOpenDuration', 'text'),
 'textBoxLane1Pos': ('PrintingMotion', 'Y_PrintPass1Position', 'text'),
 'textBoxLane2Pos': ('PrintingMotion', 'Y_PrintPass2Position', 'text'),
 'textBoxLayerThickness': ('Printing', 'LayerThickness', 'text'),
 'textBoxMinRandomization': ('Printing', 'MinRandomizationPx', 'text'),
 'textBoxMultiPassCount': ('Printing', 'MultiPassCount', 'text'),
 'textBoxPdOffset': ('Printing', 'ProductDetectOffset', 'text'),
 'textBoxPort1': ('Printing', 'Printhead1Port', 'text'),
 'textBoxPort2': ('Printing', 'Printhead2Port', 'text'),
 'textBoxPort3': ('Printing', 'Printhead3Port', 'text'),
 'textBoxPowderCheckPosition': ('PrintingMotion', 'F_PowderCheckingPosition', 'text'),
 'textBoxPreheatDuration': ('Printing', 'IRPreheatDuration', 'text'),
 'textBoxPreheatingVelocity': ('Printing', 'F_PreheatingSpeed', 'text'),
 'textBoxPrintMaxPos': ('PrintingMotion', 'X_PrintEffectiveMaxPosition', 'text'),
 'textBoxPrintMinPos': ('PrintingMotion', 'X_PrintEffectiveMinPosition', 'text'),
 'textBoxPrintingVelocity': ('Printing', 'X_PrintingSpeed', 'text'),
 'textBoxRecoatingEndPos1': ('PrintingMotion', 'F_RecoatingPass1EndPosition', 'text'),
 'textBoxRecoatingEndPos2': ('PrintingMotion', 'F_RecoatingPass2EndPosition', 'text'),
 'textBoxRecoatingStartPos1': ('PrintingMotion', 'F_RecoatingPass1StartPosition', 'text'),
 'textBoxRecoatingStartPos2': ('PrintingMotion', 'F_RecoatingPass2StartPosition', 'text'),
 'textBoxResupplyPosition': ('PrintingMotion', 'F_HopperPosition', 'text'),
 'textBoxRollerEnd1': ('PrintingMotion', 'F_RollerPass1EndPosition', 'text'),
 'textBoxRollerEnd2': ('PrintingMotion', 'F_RollerPass2EndPosition', 'text'),
 'textBoxRollerSpeed': ('Printing', 'RollerRpm', 'text'),
 'textBoxRollerStart1': ('PrintingMotion', 'F_RollerPass1StartPosition', 'text'),
 'textBoxRollerStart2': ('PrintingMotion', 'F_RollerPass2StartPosition', 'text'),
 'textBoxSliceHeight': ('Printing', 'ExpectedSliceHeight', 'text'),
 'textBoxSliceWidth': ('Printing', 'ExpectedSliceWidth', 'text'),
 'textBoxSpreadingRollerRpm': ('Printing', 'SpreadingRollerRpm', 'text'),
 'textBoxTransferDuration': ('Printing', 'CarrierToHopperDuration', 'text'),
 'textBoxVibrationDuration': ('Printing', 'HopperVibratorDuration', 'text')}

# These controls appear in the original SequenceView but are not included in
# CreateTextBindings/CreateToggleBindings, so Save does not commit their values.
UNBOUND_FIELDS = frozenset({
    "checkBoxFoundNoZMovement", "checkBoxFoundationUseHeater", "checkBoxPrintNoPrinthead",
    "checkBoxRecoatNoHeater", "checkBoxRecoatNoPowder", "checkBoxRecoatNoRoller", "checkBoxRecoatNoZMovement",
    "textBoxPrintFinishMax", "textBoxPrintFinishMin", "textBoxPrintFinishY",
})


def field_bindings(machine):
    if machine not in ("M1", "S3"):
        raise SequenceFieldAuditError("M1/S3 외 장비의 Sequence 저장 매핑은 지원하지 않습니다.")
    result = dict(FIELD_BINDINGS)
    # Neither M1 nor S3 declares the adaptive-recoating recipe fragment.
    result.pop("checkBoxAdaptiveHeating")
    if machine == "M1":
        # M1 material.adapter has no spreadingRollerRpmSource.
        result.pop("textBoxSpreadingRollerRpm")
    return result


def _decimal(value, field):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise SequenceFieldAuditError(f"{field}: 숫자 형식을 확인할 수 없습니다.")
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation as error:
        raise SequenceFieldAuditError(f"{field}: 숫자 형식/단위를 추측하지 않고 자동 적용을 거부합니다.") from error
    if not result.is_finite():
        raise SequenceFieldAuditError(f"{field}: 유한한 숫자가 아닙니다.")
    return result


def _bool(value, field, ui=False):
    if type(value) is bool:
        return value
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in ("true", "false"):
            return text == "true"
        if ui and text in ("toggle:0", "toggle:1"):
            return text == "toggle:1"
    raise SequenceFieldAuditError(f"{field}: 명확한 True/False 체크 상태가 아닙니다.")


def _parameter_index(persisted):
    index = {}
    values = getattr(persisted, "non_ir", None)
    if not isinstance(values, Mapping):
        raise SequenceFieldAuditError("기존 저장 설정의 전체 매핑을 확인할 수 없습니다.")
    for identity, typed_value in values.items():
        if not isinstance(identity, str):
            raise SequenceFieldAuditError("저장 설정 식별자가 올바르지 않습니다.")
        parts = identity.split("\x1f")
        if len(parts) == 2 and parts[1] == "__file__":
            continue
        if len(parts) != 3 or not all(parts):
            raise SequenceFieldAuditError("저장 설정의 파일/Scope/Name 식별자가 모호합니다.")
        if not isinstance(typed_value, (tuple, list)) or len(typed_value) != 2:
            raise SequenceFieldAuditError("저장 설정의 Type/Value를 확인할 수 없습니다.")
        scope_name = (parts[1], parts[2])
        if scope_name in index:
            raise SequenceFieldAuditError(f"중복된 저장 설정 {parts[1]}/{parts[2]} 때문에 자동 적용을 거부합니다.")
        index[scope_name] = typed_value
    return index


def assert_fields_match_persisted(fields, persisted, machine):
    """Reject any unsaved non-IR field before admitting an automatic UI Save.

    Every field Save actually binds must be present and have one exact persisted
    Scope/Name. Known unbound per-action/display controls are ignored; unknown
    fields, units, owner types and conversions fail closed. This performs no I/O.
    """
    bindings = field_bindings(machine)
    if not isinstance(fields, dict) or persisted is None:
        raise SequenceFieldAuditError("Sequence UI와 저장 설정을 모두 확인해야 합니다.")
    ignored = UNBOUND_FIELDS | (set(FIELD_BINDINGS) - set(bindings))
    unknown = set(fields) - set(bindings) - ignored
    missing = set(bindings) - set(fields)
    if unknown:
        raise SequenceFieldAuditError("저장 매핑이 확인되지 않은 UI 필드: " + ", ".join(sorted(str(key) for key in unknown)))
    if missing:
        raise SequenceFieldAuditError("UI에서 누락된 저장 필드: " + ", ".join(sorted(missing)))
    parameters = _parameter_index(persisted)
    for field, (scope, name, kind) in bindings.items():
        if field == "textBoxHeatingOutput":
            expected_type, saved = "DoubleParameter", persisted.power
        else:
            item = parameters.get((scope, name))
            if item is None:
                raise SequenceFieldAuditError(f"{field}: 저장된 {scope}/{name} 항목이 없습니다.")
            expected_type, saved = item
        actual = fields[field]
        if kind == "toggle":
            if expected_type != "BoolParameter":
                raise SequenceFieldAuditError(f"{field}: BoolParameter 매핑이 아닙니다.")
            same = _bool(actual, field, ui=True) == _bool(saved, field)
        elif expected_type == "IntParameter":
            if not isinstance(actual, str) or re.fullmatch(r"[+-]?[0-9]+", actual.strip()) is None:
                raise SequenceFieldAuditError(f"{field}: 원본 정수 입력 형식을 확인할 수 없습니다.")
            number = _decimal(saved, field)
            if number != number.to_integral_value():
                raise SequenceFieldAuditError(f"{field}: 저장된 정수가 올바르지 않습니다.")
            same = Decimal(actual.strip()) == number
        elif expected_type == "DoubleParameter":
            same = abs(_decimal(actual, field) - _decimal(saved, field)) <= Decimal("1e-9")
        elif expected_type == "StringParameter":
            if not isinstance(actual, str) or not isinstance(saved, str):
                raise SequenceFieldAuditError(f"{field}: 문자열 형식을 확인할 수 없습니다.")
            same = actual == saved
        else:
            raise SequenceFieldAuditError(f"{field}: 지원하지 않는 저장 타입 {expected_type!r}입니다.")
        if not same:
            raise SequenceFieldAuditError(f"{field}: UI에 저장되지 않은 편집값이 있어 자동 적용을 거부합니다. 원본에서 확인하고 다시 연결하세요.")
