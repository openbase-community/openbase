import mimetypes
import os

import requests
from django.conf import settings
from django.http import Http404, HttpResponse
from django.utils._os import safe_join
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def proxy_or_fallback(request, path=""):
    """Proxy to localhost:8081 in DEBUG mode, otherwise serve static React app."""
    if settings.DEBUG:
        try:
            # Proxy the request to localhost:8081
            target_url = f"http://localhost:8081/{path}"

            # Forward query parameters if any
            if request.GET:
                target_url += "?" + request.GET.urlencode()

            # Prepare headers (excluding problematic ones)
            headers = {}
            for key, value in request.META.items():
                if key.startswith("HTTP_"):
                    # Convert HTTP_HEADER_NAME to Header-Name format
                    header_name = key[5:].replace("_", "-").title()
                    # Skip problematic headers
                    if header_name.lower() not in ["host", "content-length"]:
                        headers[header_name] = value

            # Forward the request based on method
            if request.method == "GET":
                response = requests.get(target_url, headers=headers, timeout=30)
            elif request.method == "POST":
                response = requests.post(
                    target_url, data=request.body, headers=headers, timeout=30
                )
            elif request.method == "PUT":
                response = requests.put(
                    target_url, data=request.body, headers=headers, timeout=30
                )
            elif request.method == "DELETE":
                response = requests.delete(target_url, headers=headers, timeout=30)
            elif request.method == "PATCH":
                response = requests.patch(
                    target_url, data=request.body, headers=headers, timeout=30
                )
            else:
                # For other methods, fall back to generic request
                response = requests.request(
                    request.method,
                    target_url,
                    data=request.body,
                    headers=headers,
                    timeout=30,
                )

            # Create Django response with the proxied content
            django_response = HttpResponse(
                content=response.content,
                status=response.status_code,
                content_type=response.headers.get("content-type", "text/html"),
            )

            # Forward important response headers
            for key, value in response.headers.items():
                if key.lower() not in [
                    "content-encoding",
                    "content-length",
                    "transfer-encoding",
                ]:
                    django_response[key] = value

            return django_response

        except requests.exceptions.RequestException:
            # If proxy fails, fall back to 404
            raise Http404("File not found")
    else:
        # In production, serve the built React app
        return serve_react_app(request, path)


def serve_react_app(request, path=""):
    """Serve the built React app from the static directory."""
    # Get the directory where this module is located
    current_dir = os.path.dirname(os.path.abspath(__file__))
    static_dir = os.path.join(current_dir, "..", "static")
    static_dir = os.path.normpath(static_dir)

    # If no path or path is empty, serve index.html
    if not path or path == "":
        file_path = os.path.join(static_dir, "index.html")
    else:
        # Serve the requested static file
        file_path = safe_join(static_dir, path)

        # If the file doesn't exist, serve index.html (for SPA routing)
        if not os.path.exists(file_path):
            file_path = os.path.join(static_dir, "index.html")

    try:
        with open(file_path, "rb") as f:
            content = f.read()

        # Determine content type
        content_type, _ = mimetypes.guess_type(file_path)
        if content_type is None:
            content_type = "text/html"

        return HttpResponse(content, content_type=content_type)
    except (IOError, OSError):
        raise Http404("File not found")
