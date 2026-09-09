"""Migrate and populate current exchange classifications from existing sources."""
import argparse
from api.config.clickhouse import get_clickhouse_client
from engine.core.exchanges import refresh_exchange_codes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    client = get_clickhouse_client()
    try:
        if args.apply:
            client.command("ALTER TABLE security_master ADD COLUMN IF NOT EXISTS exchange_code LowCardinality(String) DEFAULT ''")
        print(refresh_exchange_codes(client, dry_run=not args.apply))
    finally:
        client.close()


if __name__ == "__main__":
    main()
