#!/usr/bin/env python3
"""Quick test of transcriber module"""
import sys
sys.path.insert(0, 'src')

from fcrawl.utils.transcriber import SenseVoiceTranscriber

# Test with Rick Roll
print("Testing SenseVoice transcriber...")
print("This will download the model on first run (~800MB)")

# Just test that imports work first
print("Imports successful!")
print(f"Available models: {list(SenseVoiceTranscriber.MODELS.keys())}")
