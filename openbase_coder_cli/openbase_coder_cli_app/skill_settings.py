"""Settings for local skill links and cross-device skill sharing."""

from rest_framework.decorators import api_view
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

from openbase_coder_cli import dispatcher_config, skills_autolink, skills_sync
from openbase_coder_cli.code_sync import CodeSyncError


def _payload(link_result=None, device_result=None):
    return {
        "auto_link_personal_skills": dispatcher_config.auto_link_personal_skills(),
        **skills_sync.settings_payload(),
        "link_result": link_result,
        "device_result": device_result,
    }


@api_view(["GET", "PATCH"])
def skill_sharing_settings(request):
    if request.method == "GET":
        return Response(_payload())
    allowed = {"auto_link_personal_skills", "sync_skills_across_devices"}
    if not request.data or set(request.data) - allowed:
        raise ValidationError("Provide a supported skill-sharing setting.")
    if any(type(value) is not bool for value in request.data.values()):
        raise ValidationError("Skill-sharing settings must be booleans.")
    link_result = device_result = None
    if "sync_skills_across_devices" in request.data:
        try:
            device_result = skills_sync.set_enabled(
                request.data["sync_skills_across_devices"]
            )
        except (CodeSyncError, ValueError) as exc:
            raise ValidationError(str(exc)) from exc
        except OSError as exc:
            raise APIException("Unable to save device skill sharing.") from exc
    if "auto_link_personal_skills" in request.data:
        dispatcher_config.set_auto_link_personal_skills(
            request.data["auto_link_personal_skills"]
        )
        link_result = skills_autolink.sync_auto_linked_skills()
    return Response(_payload(link_result, device_result))
