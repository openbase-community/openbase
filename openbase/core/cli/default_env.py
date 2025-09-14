import json


def make_default_env(package_name_snake: str, package_name_url_prefix: str) -> str:
    url_prefixes = {f"{package_name_snake}.*": f"api/{package_name_url_prefix}/"}

    env_vars = {
        "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/appdata",
        "DJANGO_DEBUG": "1",
        "DJANGO_SECRET_KEY": "'insecure-secret-key'",
        "LANGFUSE_HOST": '"https://us.cloud.langfuse.com"',
        "LANGFUSE_PUBLIC_KEY": "pk-lf-mykeyhere",
        "LANGFUSE_SECRET_KEY": "sk-lf-mykeyhere",
        "OPENAI_API_KEY": "sk-mykeyhere",
        "PGVECTOR_CONNECTION_STRING": '"postgresql+psycopg://postgres:postgres@localhost:5432/appdata"',
        "REDIS_URL": "redis://localhost:6379/0",
        "SENDGRID_API_KEY": "SG.mykeyhere",
        "SESSION_AUTH": "1",
        "STRIPE_PRODUCT_ID": "prod_myproductid",
        "STRIPE_SECRET_KEY": "sk_test_mysecretkey",
        "STRIPE_WEBHOOK_SECRET": "whsec_mywebhooksecret",
        "URL_PREFIXES": f"'{json.dumps(url_prefixes)}'",
        "OPENBASE_SECRET_KEY": "'insecure-secret-key'",
        "OPENBASE_ALLOWED_HOSTS": "hot-zebra-freely.ngrok-free.app",
        "OPENBASE_DEBUG": "1",
    }

    lines = [f"{key}={value}" for key, value in env_vars.items()]
    return "\n".join(lines) + "\n"
