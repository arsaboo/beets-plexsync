#!/usr/bin/env python3
"""Test script to validate improved match scoring with example cases."""

import sys
import os

# Add the harmony package to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'harmony'))

from harmony.core.matching import plex_track_distance

def test_example_1():
    """Test Example 1: Dil Dil Dil case where candidate 4 should score highest."""
    print("=== Test Example 1: Dil Dil Dil ===")
    
    query = {
        "title": "DIL DIL DIL",
        "artist": "Harshvardhan & Sonam | Sunidhi | Divya | Rajat | Anu",
        "album": "Ek Deewane Ki Deewaniyat"
    }
    
    candidates = [
        {
            "title": "Deewane deewani",
            "artist": "Alisha Chinai",
            "album": "Satya Bol",
            "description": "Candidate 1 (WRONG) - previously scored 0.29"
        },
        {
            "title": "Deewaniyat (Unplugged)",
            "artist": "Vishal Mishra",
            "album": "Ek Deewane Ki Deewaniyat",
            "description": "Candidate 2 (WRONG) - previously scored 0.26"
        },
        {
            "title": "Deewaniyat",
            "artist": "Vishal Mishra, Kaushik-Guddu, Kunaal Vermaa",
            "album": "Ek Deewane Ki Deewaniyat",
            "description": "Candidate 3 (WRONG) - previously scored 0.26"
        },
        {
            "title": "Dil Dil Dil",
            "artist": "Sunidhi Chauhan, Rahat Nagpal, Anu Malik",
            "album": "Ek Deewane Ki Deewaniyat",
            "description": "Candidate 4 (CORRECT) - previously scored 0.25"
        },
        {
            "title": "Deewane To Deewane Hain",
            "artist": "Shweta Shetty",
            "album": "Deewane To Deewane Hain",
            "description": "Candidate 5 (WRONG) - previously scored 0.23"
        }
    ]
    
    results = []
    for i, candidate in enumerate(candidates):
        match_score = plex_track_distance(query, candidate)
        results.append((i, candidate["description"], match_score.similarity, match_score))
        print(f"Candidate {i}: {match_score.similarity:.3f} - {candidate['description']}")
        print(f"  Details: {match_score.details}")
        print()
    
    # Sort by score
    results.sort(key=lambda x: x[2], reverse=True)
    print("=== Results (sorted by score) ===")
    for i, desc, score, match_score in results:
        print(f"Score {score:.3f}: {desc}")
    
    # Check if correct candidate (index 3) is highest
    best_idx = results[0][0]
    if best_idx == 3:
        print("\nSUCCESS: Correct candidate (4) scored highest!")
    else:
        print(f"\nFAILURE: Candidate {best_idx + 1} scored highest instead of candidate 4")
    
    return results

def test_example_2():
    """Test Example 2: Tu Meri Main Tera case where candidate 2 should score highest."""
    print("\n=== Test Example 2: Tu Meri Main Tera ===")
    
    query = {
        "title": "Tu Meri Main Tera Main Tera Tu Meri - Title Track",
        "artist": "Vishal-Shekhar, Vishal Dadlani, Shekhar Ravjiani, Anvita Dutt",
        "album": "Tu Meri Main Tera Main Tera Tu Meri"
    }
    
    candidates = [
        {
            "title": "Tu Meri Main Tera Main Tera Tu Meri - Tu Meri Main Tera Main Tera Tu Meri - Title Track",
            "artist": "Vishal-Shekhar, Vishal Dadlani, Shekhar Ravjiani, Anvita Dutt",
            "album": "Tu Meri Main Tera Main Tera Tu Meri",
            "description": "Candidate 1 (WRONG) - previously scored 0.64"
        },
        {
            "title": "Tu Meri Main Tera Main Tera Tu Meri - Hum Dono",
            "artist": "Vishal-Shekhar, Shekhar Ravjiani, Shruti Pathak, Vishal Dadlani, Anvita Dutt",
            "album": "Tu Meri Main Tera Main Tera Tu Meri",
            "description": "Candidate 2 (CORRECT) - previously scored 0.50"
        }
    ]
    
    results = []
    for i, candidate in enumerate(candidates):
        match_score = plex_track_distance(query, candidate)
        results.append((i, candidate["description"], match_score.similarity, match_score))
        print(f"Candidate {i}: {match_score.similarity:.3f} - {candidate['description']}")
        print(f"  Details: {match_score.details}")
        print()
    
    # Sort by score
    results.sort(key=lambda x: x[2], reverse=True)
    print("=== Results (sorted by score) ===")
    for i, desc, score, match_score in results:
        print(f"Score {score:.3f}: {desc}")
    
    # Check if correct candidate (index 1) is highest
    best_idx = results[0][0]
    if best_idx == 1:
        print("\nSUCCESS: Correct candidate (2) scored highest!")
    else:
        print(f"\nFAILURE: Candidate {best_idx + 1} scored highest instead of candidate 2")
    
    return results

def test_word_aware_matching():
    """Test the word-aware matching function directly."""
    print("\n=== Test Word-Aware Matching ===")
    
    from harmony.core.matching import calculate_word_aware_similarity
    
    test_cases = [
        # Exact matches
        ("dil dil dil", "dil dil dil", 1.0),
        ("sunidhi", "sunidhi", 1.0),
        
        # Complete word matches (should score high)
        ("dil", "dil dil dil", 0.85),
        ("sunidhi", "sunidhi chauhan", 0.85),
        ("anu", "anu malik", 0.85),
        
        # Partial word matches (should score low)
        ("dil", "deewani", 0.25),
        ("deewani", "deewaniyat", 0.35),
        
        # No matches
        ("random", "completely", 0.1),
    ]
    
    for source, target, expected_range in test_cases:
        score = calculate_word_aware_similarity(source, target)
        status = "PASS" if abs(score - expected_range) < 0.2 else "FAIL"
        print(f"{status} '{source}' vs '{target}': {score:.3f} (expected ~{expected_range})")

if __name__ == "__main__":
    print("Testing improved match scoring implementation...")
    
    try:
        # Test word-aware matching first
        test_word_aware_matching()
        
        # Test example cases
        results1 = test_example_1()
        results2 = test_example_2()
        
        print("\n=== Summary ===")
        print("All tests completed. Check results above for scoring improvements.")
        
    except Exception as e:
        print(f"Error during testing: {e}")
        import traceback
        traceback.print_exc()