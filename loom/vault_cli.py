"""Command-line helper for adding an API key to the vault. Run from loom/:
    python vault_cli.py gemini-flash
Prompts for the key (hidden input), writes it to .env, and validates it
against the real API before confirming.
"""

import sys
import getpass

from config import vault
from providers.config import MODEL_REGISTRY


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in MODEL_REGISTRY:
        print(f"Usage: python vault_cli.py <model_key>\nKnown model keys: {sorted(MODEL_REGISTRY)}")
        sys.exit(1)

    model_key = sys.argv[1]
    value = getpass.getpass(f"API key for {model_key!r} (hidden input): ").strip()
    if not value:
        print("Empty key, not saving.")
        sys.exit(1)

    vault.set_key(model_key, value)
    print("Saved to .env. Validating against the real API...")
    if vault.validate_key(model_key, force=True):
        print(f"✓ {model_key} is valid and ready to use.")
    else:
        print(f"✗ Saved, but validation failed — double-check the key is correct.")


if __name__ == "__main__":
    main()
