import unittest
import json
from datetime import timedelta
from senpuki.core import Result, RetryPolicy, compute_retry_delay
from senpuki.utils.serialization import JsonSerializer
from senpuki.utils.time import parse_duration

class TestCore(unittest.TestCase):
    def test_result_serialization(self):
        serializer = JsonSerializer()
        
        # Test Ok
        r1 = Result.Ok({"foo": "bar"})
        encoded = serializer.dumps(r1)
        decoded = serializer.loads(encoded)
        self.assertTrue(decoded.ok)
        self.assertEqual(decoded.value, {"foo": "bar"})
        self.assertIsNone(decoded.error)

        # Test Error
        r2 = Result.Error("Something bad")
        encoded = serializer.dumps(r2)
        decoded = serializer.loads(encoded)
        self.assertFalse(decoded.ok)
        self.assertEqual(decoded.error, "Something bad")
        
    def test_retry_policy_calculation(self):
        policy = RetryPolicy(
            initial_delay=1.0,
            backoff_factor=2.0,
            jitter=0.0 # disable jitter for deterministic check
        )
        
        # attempt 1: 1.0 * (2^(1-1)) = 1.0
        self.assertEqual(compute_retry_delay(policy, 1), 1.0)
        # attempt 2: 1.0 * (2^(2-1)) = 2.0
        self.assertEqual(compute_retry_delay(policy, 2), 2.0)
        # attempt 3: 1.0 * (2^(3-1)) = 4.0
        self.assertEqual(compute_retry_delay(policy, 3), 4.0)
        
    def test_retry_policy_jitter(self):
        policy = RetryPolicy(
            initial_delay=1.0,
            jitter=0.1
        )
        # Should be between 0.9 and 1.1
        delay = compute_retry_delay(policy, 1)
        self.assertTrue(0.9 <= delay <= 1.1)

    def test_parse_duration(self):
        self.assertEqual(parse_duration("30s"), timedelta(seconds=30))
        self.assertEqual(parse_duration("5m"), timedelta(minutes=5))
        self.assertEqual(parse_duration("1h"), timedelta(hours=1))
