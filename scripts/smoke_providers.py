#!/usr/bin/env python3
"""Quick test of Ratatoskr provider layer."""

import asyncio
import os
import sys

from api_agent.llm import create_provider


async def test_provider(provider_name: str):
    """Test basic provider creation and completion."""
    print(f"\n{'='*60}")
    print(f"Testing {provider_name} provider")
    print(f"{'='*60}")
    
    try:
        provider = create_provider(provider=provider_name)
        print(f"✓ Provider created: {provider.__class__.__name__}")
        print(f"  Model: {provider.model}")
        
        # Test simple completion (no tools)
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Say 'Hello from Ratatoskr!' and nothing else."},
        ]
        
        response = await provider.complete(messages, temperature=0.0, max_tokens=50)
        
        print(f"✓ Completion successful")
        print(f"  Content: {response.content[:100] if response.content else '(none)'}")
        print(f"  Tool calls: {len(response.tool_calls)}")
        if response.usage:
            print(f"  Tokens: {response.usage.get('total_tokens', '?')}")
        
        return True
    
    except Exception as e:
        print(f"✗ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Test all configured providers."""
    results = {}
    
    # OpenAI
    if os.getenv("OPENAI_API_KEY"):
        results["openai"] = await test_provider("openai")
    else:
        print("\n⊘ Skipping OpenAI (no API key)")
    
    # Anthropic
    if os.getenv("ANTHROPIC_API_KEY"):
        results["anthropic"] = await test_provider("anthropic")
    else:
        print("\n⊘ Skipping Anthropic (no API key)")
    
    # OpenAI-compatible (needs base_url + model)
    if os.getenv("OPENAI_BASE_URL") and "localhost" in os.getenv("OPENAI_BASE_URL", ""):
        print("\n⊘ Skipping openai-compat (local endpoint, untested)")
    else:
        print("\n⊘ Skipping openai-compat (no local endpoint)")
    
    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for name, success in results.items():
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"  {name:15} {status}")
    
    all_pass = all(results.values())
    if all_pass:
        print("\n✓ All providers passed")
        return 0
    else:
        print("\n✗ Some providers failed")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
