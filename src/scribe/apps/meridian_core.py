"""App profile for MERIDIAN CORE.

Three kinds of knowledge belong to the *product*, not to any one capability:

  * how you sign on,
  * which interruptions are routine and how to clear them,
  * how the app words its own failures.

If every capability re-derived those, a thousand artifacts would each carry
their own slightly different opinion of what "session expired" looks like, and
fixing one would fix none of the others. So they are authored here and stamped
onto every capability recorded against this product. A tenant overlay can add to
them; nothing has to re-record because of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..types.actions import Click, Fill, Navigate, StepAction
from ..types.artifact import (
    AppBinding,
    EnvironmentSignal,
    ErrorPolicy,
    RecoveryRule,
    Step,
)
from ..types.conditions import AllOf, AnyOf, Condition, Not, TextPresent, UrlMatches
from ..types.core import (
    STRATEGY_CONFIDENCE,
    ControlRef,
    LocatorCandidate,
    LocatorStrategy,
    RiskTier,
    SecretValue,
    SurfaceKind,
)
from ..types.results import FailureClass


def control(role: str, name: str, frame: list[str] | None = None) -> ControlRef:
    """A control described the way an operator would describe it."""
    frame = frame or []
    return ControlRef(
        role=role, name=name, frame_path=frame,
        candidates=[
            LocatorCandidate(
                strategy=LocatorStrategy.ROLE_NAME,
                args={"role": role, "name": name, "frame_path": frame},
                confidence=STRATEGY_CONFIDENCE[LocatorStrategy.ROLE_NAME],
                note="accessible role and name"),
            LocatorCandidate(
                strategy=LocatorStrategy.LABEL_ADJACENT,
                args={"label": name, "role": role, "frame_path": frame},
                confidence=STRATEGY_CONFIDENCE[LocatorStrategy.LABEL_ADJACENT],
                note="caption rendered beside the control"),
        ])


SIGN_ON_VISIBLE: Condition = TextPresent(text="Operator Sign On")


@dataclass(frozen=True)
class AppProfile:
    app_id: str
    vendor_product: str
    product_version: str
    surface: SurfaceKind
    entrypoint: str
    login: list[Step] = field(default_factory=list)
    recovery: list[RecoveryRule] = field(default_factory=list)
    environment: list[EnvironmentSignal] = field(default_factory=list)
    # Sign-on controls, so the discovery loop never has to reason about
    # credentials -- it starts already signed on.
    login_secrets: tuple[str, ...] = ()

    def binding(self, tenant_id: str | None = None) -> AppBinding:
        return AppBinding(
            app_id=self.app_id, vendor_product=self.vendor_product,
            product_version=self.product_version, surface=self.surface,
            entrypoint=self.entrypoint, scope="product", recorded_on_tenant=tenant_id)


_LOGIN: list[Step] = [
    Step(id="open_app", intent="open the console",
         action=Navigate(url_template="{base_url}/"),
         postcondition=AnyOf(conditions=[SIGN_ON_VISIBLE,
                                         TextPresent(text="Member Services")])),
    Step(id="enter_operator_id", intent="enter the service operator id",
         action=Fill(target=control("textbox", "Operator ID"),
                     value=SecretValue(secret="core_operator_id")),
         precondition=SIGN_ON_VISIBLE),
    Step(id="enter_passcode", intent="enter the service passcode",
         action=Fill(target=control("textbox", "Passcode"),
                     value=SecretValue(secret="core_passcode")),
         precondition=SIGN_ON_VISIBLE),
    Step(id="sign_on", intent="sign on to the console",
         action=Click(target=control("button", "Sign On")),
         precondition=SIGN_ON_VISIBLE,
         risk=RiskTier.SAFE,
         postcondition=AnyOf(conditions=[UrlMatches(pattern=r"/console"),
                                         TextPresent(text="Member Services")]),
         on_error=ErrorPolicy(retries=1, backoff_ms=750)),
]


def _reauth_actions() -> list[StepAction]:
    return [
        Fill(target=control("textbox", "Operator ID"),
             value=SecretValue(secret="core_operator_id")),
        Fill(target=control("textbox", "Passcode"),
             value=SecretValue(secret="core_passcode")),
        Click(target=control("button", "Sign On")),
    ]


_RECOVERY: list[RecoveryRule] = [
    RecoveryRule(
        id="dismiss_system_notice",
        description="acknowledge an unscheduled system notice and carry on",
        when=TextPresent(text="System Notice"),
        do=[Click(target=control("button", "Acknowledge"))],
        max_uses=3),
    RecoveryRule(
        id="reauthenticate",
        description="sign back on after the core drops the session mid-flow",
        when=TextPresent(text="session has expired"),
        do=_reauth_actions(),
        max_uses=1,
        # Signing back on lands on the console home, not where we were, so the
        # flow has to restart from its first business step rather than blindly
        # re-trying whatever it was doing.
        resume_at_step="__first_step__"),
]


_ENVIRONMENT: list[EnvironmentSignal] = [
    EnvironmentSignal(
        id="app_error",
        description="the core returned an application error",
        # This core renders *every* fault through the same "Application Error"
        # chrome, including the privacy-hold refusal below. Discriminating on
        # the error code rather than on list order keeps the two apart no matter
        # how the signals are ordered -- and code is what the vendor documents.
        when=AllOf(conditions=[TextPresent(text="Application Error"),
                               Not(condition=TextPresent(text="SEC-0041"))]),
        failure_class=FailureClass.APP_ERROR,
        remediation="transient core error; retry the capability, and raise a ticket "
                    "with the reference on screen if it persists"),
    EnvironmentSignal(
        id="permission_denied",
        description="this operator is not authorized to service the record",
        when=AnyOf(conditions=[
            TextPresent(text="SEC-0041"),
            TextPresent(text="not authorized to service this member")]),
        failure_class=FailureClass.PERMISSION_DENIED,
        remediation="the record is under a privacy hold; a supervisor has to service it"),
    EnvironmentSignal(
        id="session_lost",
        description="the console session expired and could not be re-established",
        when=TextPresent(text="session has expired"),
        failure_class=FailureClass.SESSION_LOST,
        remediation="re-authentication already ran and did not take; check the "
                    "service operator credentials"),
]


MERIDIAN_CORE = AppProfile(
    app_id="meridian_core",
    vendor_product="MERIDIAN CORE",
    product_version="4.2",
    surface=SurfaceKind.LEGACY_WEB,
    entrypoint="{base_url}/",
    login=_LOGIN,
    recovery=_RECOVERY,
    environment=_ENVIRONMENT,
    login_secrets=("core_operator_id", "core_passcode"),
)

PROFILES: dict[str, AppProfile] = {MERIDIAN_CORE.app_id: MERIDIAN_CORE}


def profile_for(app_id: str) -> AppProfile:
    if app_id not in PROFILES:
        raise KeyError(f"no app profile for {app_id!r}")
    return PROFILES[app_id]
