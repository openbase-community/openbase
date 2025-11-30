from __future__ import annotations

import json


def make_default_env(**kwargs) -> str:
    package_name_snake = kwargs["package_name_snake"]
    package_name_url_prefix = kwargs["package_name_url_prefix"]
    url_prefixes = {f"{package_name_snake}.*": f"api/{package_name_url_prefix}/"}

    env_vars = {
        "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/appdata",
        "DJANGO_DEBUG": "1",
        "DJANGO_SECRET_KEY": f"'{kwargs['django_secret_key']}'",
        "DJANGO_ADMIN_SUFFIX": "fill me in in production to hide the admin page",
        "LANGFUSE_HOST": '"https://us.cloud.langfuse.com"',
        "LANGFUSE_PUBLIC_KEY": "pk-lf-mykeyhere",
        "LANGFUSE_SECRET_KEY": "sk-lf-mykeyhere",
        "OPENAI_API_KEY": "sk-mykeyhere",
        "REDIS_URL": "redis://localhost:6379/0",
        "SENDGRID_API_KEY": "SG.mykeyhere",
        "SESSION_AUTH": "1",
        "STRIPE_PRODUCT_ID": "prod_myproductid",
        "STRIPE_SECRET_KEY": "sk_test_mysecretkey",
        "STRIPE_WEBHOOK_SECRET": "whsec_mywebhooksecret",
        "URL_PREFIXES": f"'{json.dumps(url_prefixes)}'",
        "OPENBASE_SECRET_KEY": f"'{kwargs['openbase_secret_key']}'",
        "OPENBASE_API_TOKEN": f"'{kwargs['openbase_api_token']}'",
        "OPENBASE_ALLOWED_HOSTS": "localhost",
        "OPENBASE_DEBUG": "1",
    }

    lines = [f"{key}={value}" for key, value in env_vars.items()]
    return "\n".join(lines) + "\n"
