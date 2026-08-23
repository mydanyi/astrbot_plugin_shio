from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum

from .contracts import ContractViolation, SenderKind
from .observability import diagnostic_digest


class BotTrustSource(str, Enum):
    """Closed authority sources for identifying a bot sender."""

    NONE = "none"
    CURRENT_ADAPTER_SELF = "current_adapter_self"
    EXPLICIT_CONFIGURATION = "explicit_configuration"
    TYPED_ADAPTER_FLAG = "typed_adapter_flag"


def _structural_id(value: object, field_name: str, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractViolation(f"{field_name}_invalid")
    normalized = value.strip()
    if not normalized:
        if optional:
            return ""
        raise ContractViolation(f"{field_name}_required")
    if normalized == "*":
        raise ContractViolation(f"{field_name}_wildcard_forbidden")
    return normalized


@dataclass(frozen=True, slots=True)
class _BotIdentity:
    platform_id: str = field(repr=False)
    sender_id: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "platform_id",
            _structural_id(self.platform_id, "platform_id"),
        )
        object.__setattr__(
            self,
            "sender_id",
            _structural_id(self.sender_id, "sender_id"),
        )


_TYPED_ADAPTER_FLAG_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class TypedAdapterBotFlag:
    """Opaque bot signal issued only by a trusted AstrBot adapter bridge."""

    platform_id: str = field(repr=False)
    sender_id: str = field(repr=False)
    is_bot: bool
    _seal: object = field(repr=False, compare=False)


def issue_typed_adapter_bot_flag(
    *,
    platform_id: str,
    sender_id: str,
    is_bot: bool,
) -> TypedAdapterBotFlag:
    """Issue a sender-bound flag; visible message fields never reach this API."""

    identity = _BotIdentity(platform_id=platform_id, sender_id=sender_id)
    if type(is_bot) is not bool:
        raise ContractViolation("adapter_bot_flag_invalid")
    flag = object.__new__(TypedAdapterBotFlag)
    object.__setattr__(flag, "platform_id", identity.platform_id)
    object.__setattr__(flag, "sender_id", identity.sender_id)
    object.__setattr__(flag, "is_bot", is_bot)
    object.__setattr__(flag, "_seal", _TYPED_ADAPTER_FLAG_SEAL)
    return flag


def _is_issued_adapter_flag(value: object) -> bool:
    return (
        isinstance(value, TypedAdapterBotFlag)
        and getattr(value, "_seal", None) is _TYPED_ADAPTER_FLAG_SEAL
    )


@dataclass(frozen=True, slots=True)
class BotIdentityObservation:
    """Trusted structural inputs used by the registry.

    Nicknames, group cards, message text, quoted claims and model output are
    deliberately absent from this type.
    """

    platform_id: str = field(repr=False)
    sender_id: str = field(repr=False)
    current_adapter_self_id: str = field(default="", repr=False)
    adapter_bot_flag: TypedAdapterBotFlag | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "platform_id",
            _structural_id(self.platform_id, "platform_id"),
        )
        object.__setattr__(
            self,
            "sender_id",
            _structural_id(self.sender_id, "sender_id"),
        )
        object.__setattr__(
            self,
            "current_adapter_self_id",
            _structural_id(
                self.current_adapter_self_id,
                "current_adapter_self_id",
                optional=True,
            ),
        )
        if self.adapter_bot_flag is not None and not _is_issued_adapter_flag(
            self.adapter_bot_flag
        ):
            raise ContractViolation("typed_adapter_bot_flag_required")


@dataclass(frozen=True, slots=True)
class BotTrustDecision:
    """Deterministic registry result; an unknown sender is not presumed human."""

    platform_id: str = field(repr=False)
    sender_id: str = field(repr=False)
    sender_kind: SenderKind
    source: BotTrustSource
    adapter_flag_present: bool = False
    adapter_flag_bound: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "platform_id",
            _structural_id(self.platform_id, "platform_id"),
        )
        object.__setattr__(
            self,
            "sender_id",
            _structural_id(self.sender_id, "sender_id"),
        )
        if not isinstance(self.sender_kind, SenderKind):
            raise ContractViolation("sender_kind_invalid")
        if not isinstance(self.source, BotTrustSource):
            raise ContractViolation("bot_trust_source_invalid")
        if type(self.adapter_flag_present) is not bool or type(
            self.adapter_flag_bound
        ) is not bool:
            raise ContractViolation("adapter_flag_state_invalid")
        expected_kind = {
            BotTrustSource.NONE: SenderKind.UNKNOWN,
            BotTrustSource.CURRENT_ADAPTER_SELF: SenderKind.SELF,
            BotTrustSource.EXPLICIT_CONFIGURATION: SenderKind.KNOWN_BOT,
            BotTrustSource.TYPED_ADAPTER_FLAG: SenderKind.KNOWN_BOT,
        }[self.source]
        if self.sender_kind is not expected_kind:
            raise ContractViolation("bot_trust_sender_kind_mismatch")
        if self.source is BotTrustSource.TYPED_ADAPTER_FLAG and not (
            self.adapter_flag_present and self.adapter_flag_bound
        ):
            raise ContractViolation("typed_adapter_flag_not_bound")

    @property
    def is_trusted_bot(self) -> bool:
        return self.source is not BotTrustSource.NONE

    def trace_metadata(self) -> dict[str, str | bool]:
        return {
            "bot_trusted": self.is_trusted_bot,
            "sender_kind": self.sender_kind.value,
            "bot_trust_source": self.source.value,
            "platform_digest": diagnostic_digest(self.platform_id),
            "sender_digest": diagnostic_digest(self.sender_id),
            "adapter_flag_present": self.adapter_flag_present,
            "adapter_flag_bound": self.adapter_flag_bound,
        }


@dataclass(frozen=True, slots=True)
class TrustedBotRegistry:
    """Immutable exact-match registry for trusted bot identities."""

    _configured_bots: frozenset[_BotIdentity] = field(
        default_factory=frozenset,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self._configured_bots, frozenset) or any(
            not isinstance(identity, _BotIdentity)
            for identity in self._configured_bots
        ):
            raise ContractViolation("trusted_bot_configuration_invalid")

    @classmethod
    def from_configured_pairs(
        cls,
        configured_pairs: Iterable[Sequence[object]],
    ) -> "TrustedBotRegistry":
        if isinstance(configured_pairs, (str, bytes)):
            raise ContractViolation("trusted_bot_configuration_invalid")
        try:
            raw_pairs = tuple(configured_pairs)
        except TypeError as exc:
            raise ContractViolation("trusted_bot_configuration_invalid") from exc

        identities: set[_BotIdentity] = set()
        for pair in raw_pairs:
            if isinstance(pair, (str, bytes)) or not isinstance(pair, Sequence):
                raise ContractViolation("trusted_bot_pair_invalid")
            if len(pair) != 2:
                raise ContractViolation("trusted_bot_pair_invalid")
            identities.add(_BotIdentity(platform_id=pair[0], sender_id=pair[1]))
        return cls(frozenset(identities))

    def resolve(self, observation: BotIdentityObservation) -> BotTrustDecision:
        if not isinstance(observation, BotIdentityObservation):
            raise ContractViolation("bot_identity_observation_required")

        flag = observation.adapter_bot_flag
        flag_present = flag is not None
        flag_bound = bool(
            flag_present
            and flag.platform_id == observation.platform_id
            and flag.sender_id == observation.sender_id
        )
        if (
            observation.current_adapter_self_id
            and observation.sender_id == observation.current_adapter_self_id
        ):
            source = BotTrustSource.CURRENT_ADAPTER_SELF
            sender_kind = SenderKind.SELF
        elif _BotIdentity(
            observation.platform_id,
            observation.sender_id,
        ) in self._configured_bots:
            source = BotTrustSource.EXPLICIT_CONFIGURATION
            sender_kind = SenderKind.KNOWN_BOT
        elif flag_bound and flag is not None and flag.is_bot:
            source = BotTrustSource.TYPED_ADAPTER_FLAG
            sender_kind = SenderKind.KNOWN_BOT
        else:
            source = BotTrustSource.NONE
            sender_kind = SenderKind.UNKNOWN

        return BotTrustDecision(
            platform_id=observation.platform_id,
            sender_id=observation.sender_id,
            sender_kind=sender_kind,
            source=source,
            adapter_flag_present=flag_present,
            adapter_flag_bound=flag_bound,
        )

    def trace_metadata(self) -> dict[str, int]:
        return {"trusted_bot_config_count": len(self._configured_bots)}


__all__ = [
    "BotIdentityObservation",
    "BotTrustDecision",
    "BotTrustSource",
    "TrustedBotRegistry",
    "TypedAdapterBotFlag",
    "issue_typed_adapter_bot_flag",
]
