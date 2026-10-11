"""Preserve gateway stop reasons before the SDK replaces them with APIError."""

from openbase_coder_cli.plan_copy import SUBSCRIBE_PRO_LINE


class LiveGatewayStop(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def gateway_stop_from_error(error: dict) -> LiveGatewayStop | None:
    # The gateway retains this fatal SDK code to prevent old clients from
    # reconnecting forever. Its type distinguishes billing from other stops.
    if error.get("code") != "billing_hard_limit_reached":
        return None
    detail = str(error.get("message") or "").strip()
    reason = error.get("type")
    if not reason:
        # Compatibility with gateways deployed before structured stop reasons.
        if detail.startswith("Ending the call since it's been quiet for a while."):
            reason = "live_voice_idle_timeout"
        elif detail.startswith("Live voice calls end after "):
            reason = "live_voice_max_duration"
        elif detail.startswith("Live voice usage metering is temporarily unavailable"):
            reason = "live_voice_metering_unavailable"
        else:
            reason = "subscription_required"
    if reason not in {
        "subscription_required",
        "live_voice_idle_timeout",
        "live_voice_max_duration",
        "live_voice_metering_unavailable",
    }:
        return None
    if reason == "subscription_required" and SUBSCRIBE_PRO_LINE not in detail:
        detail = f"{detail} {SUBSCRIBE_PRO_LINE}".strip()
    return LiveGatewayStop(reason, detail or "The live voice call has ended.")
