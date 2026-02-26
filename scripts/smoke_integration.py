#!/usr/bin/env python3
"""Integration test — verify REST agent works with the new provider layer."""

import asyncio
import os

from api_agent.agent.rest_agent import process_rest_query
from api_agent.context import RequestContext


async def test_rest_agent():
    """Test REST agent with a simple OpenAPI spec (petstore)."""
    print("Testing REST agent with provider layer...")
    
    # Use the petstore OpenAPI spec (public, stable)
    ctx = RequestContext(
        target_url="https://petstore3.swagger.io/api/v3/openapi.json",
        target_headers={},
        api_type="rest",
        poll_paths=(),
        allow_unsafe_paths=(),
        base_url=None,
        include_result=True,
    )
    
    question = "List the first 3 pets"
    
    try:
        result = await process_rest_query(question, ctx)
        
        print("\n" + "="*60)
        print("RESULT")
        print("="*60)
        print(f"Success: {result.get('ok')}")
        data_str = str(result.get('data', '(none)'))
        print(f"Data: {data_str[:200]}")
        print(f"API Calls: {len(result.get('api_calls', []))}")
        
        if result.get("ok"):
            print("\n✓ Integration test PASSED")
            return True
        else:
            print(f"\n✗ Integration test FAILED: {result.get('error')}")
            return False
    
    except Exception as e:
        print(f"\n✗ Integration test FAILED with exception: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("⊘ Skipping integration test (no OPENAI_API_KEY)")
        return 0
    
    success = await test_rest_agent()
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
