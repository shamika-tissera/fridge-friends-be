"""Check that the configured LLM endpoint actually answers.

    python -m scripts.check_llm                 # uses LLM_MODEL from .env
    python -m scripts.check_llm openai/gpt-oss-20b

Prints the raw ingredient JSON, so you can tell a network problem from a
model-behaviour problem without going through the API.
"""

import json
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from app import llm  # noqa: E402  (must follow load_dotenv)


def main() -> int:
    if len(sys.argv) > 1:
        llm.MODEL = sys.argv[1]

    print(f"endpoint: {llm.BASE_URL}")
    print(f"model:    {llm.MODEL}")
    print(f"timeout:  {llm.TIMEOUT:.0f}s\n")

    started = time.time()
    try:
        result = llm.extract_ingredients(["Pad Thai"])
    except llm.LLMUnavailable as exc:
        print(f"FAILED after {time.time() - started:.0f}s: {exc}")
        print(
            "\nIf this is a connection error, try a known-fast model to separate\n"
            "a network problem from a slow one:\n"
            "    python -m scripts.check_llm openai/gpt-oss-20b"
        )
        return 1

    print(f"OK in {time.time() - started:.0f}s\n")
    for name, entry in result.items():
        print(f"{name} ({entry.cuisine})")
        for ing in entry.ingredients:
            mark = "*" if ing.essential else " "
            print(f"  {mark} {ing.name} [{ing.category}]")
    print("\nraw:", json.dumps({k: v.model_dump() for k, v in result.items()})[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
