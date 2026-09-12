"""Create private local-only Langfuse credentials; never overwrite an existing file."""
import argparse
import base64
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    values = {key: secrets.token_hex(32) for key in (
        'POSTGRES_PASSWORD', 'SALT', 'ENCRYPTION_KEY', 'CLICKHOUSE_PASSWORD',
        'REDIS_AUTH', 'MINIO_ROOT_PASSWORD', 'NEXTAUTH_SECRET', 'LANGFUSE_USER_PASSWORD')}
    values['LANGFUSE_PUBLIC_KEY'] = 'pk-lf-' + secrets.token_hex(16)
    values['LANGFUSE_SECRET_KEY'] = 'sk-lf-' + secrets.token_hex(16)
    pair = values['LANGFUSE_PUBLIC_KEY'] + ':' + values['LANGFUSE_SECRET_KEY']
    values['CORD_LANGFUSE_AUTH'] = base64.b64encode(pair.encode()).decode()
    args.path.parent.mkdir(parents=True, exist_ok=True)
    import os
    fd = os.open(args.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(''.join(f'{key}={value}\n' for key, value in values.items()))


if __name__ == '__main__':
    main()
