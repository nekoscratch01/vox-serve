#!/usr/bin/env python3
"""
Benchmarking goodput in online serving scenario.

Measures performance metrics:
- TTFA (Time to First Audio): Latency from request start to first audio chunk
- RTF (Real-Time Factor) attainment: Whether the generation is faster than real-time

Usage:
    python goodput.py --host localhost --port 8000 --rate 10 --duration 60
"""

import argparse
import asyncio
import io
import random
import statistics
import time
import wave
from dataclasses import dataclass
from typing import List, Optional

import aiohttp
import numpy as np

# Set random seed for reproducible results
random.seed(42)
np.random.seed(42)


@dataclass
class RequestMetrics:
    """Metrics for a single request."""

    request_id: str
    start_time: float
    ttfa: Optional[float] = None  # Time to first audio
    end_time: Optional[float] = None
    total_latency: Optional[float] = None
    audio_duration: Optional[float] = None  # Duration of generated audio
    rtf: Optional[float] = None  # Real-time factor
    success: bool = False
    error_message: Optional[str] = None


@dataclass
class BenchmarkResults:
    """Aggregated benchmark results."""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0

    # TTFA metrics (seconds)
    ttfa_mean: float = 0.0
    ttfa_p50: float = 0.0
    ttfa_p90: float = 0.0
    ttfa_p95: float = 0.0
    ttfa_p99: float = 0.0
    ttfa_min: float = 0.0
    ttfa_max: float = 0.0

    # RTF metrics
    rtf_mean: float = 0.0
    rtf_p50: float = 0.0
    rtf_p90: float = 0.0
    rtf_p95: float = 0.0
    rtf_p99: float = 0.0
    rtf_min: float = 0.0
    rtf_max: float = 0.0

    # Total latency metrics (seconds)
    latency_mean: float = 0.0
    latency_p50: float = 0.0
    latency_p90: float = 0.0
    latency_p95: float = 0.0
    latency_p99: float = 0.0
    latency_min: float = 0.0
    latency_max: float = 0.0


class BenchmarkClient:
    """Client for benchmarking vox-serve TTS server."""

    def __init__(self, host: str, port: int):
        self.base_url = f"http://{host}:{port}"
        self.metrics: List[RequestMetrics] = []

        # Sample texts for generation
        self.sample_texts = [
            "Hello world, this is a test message for benchmarking.",
        ]

    def generate_random_text(self, min_words: int = 5, max_words: int = 20) -> str:
        """Generate random text for testing."""
        return random.choice(self.sample_texts)

    def get_audio_duration(self, audio_data: bytes) -> float:
        """Calculate audio duration from WAV data."""
        try:
            with io.BytesIO(audio_data) as audio_io:
                with wave.open(audio_io, "rb") as wav_file:
                    frames = wav_file.getnframes()
                    sample_rate = wav_file.getframerate()
                    duration = frames / sample_rate
                    return duration
        except Exception:
            # Fallback estimation: assume 16kHz sample rate
            # WAV header is typically 44 bytes
            audio_samples = len(audio_data) - 44
            sample_rate = 16000
            bytes_per_sample = 2  # 16-bit audio
            return audio_samples / (sample_rate * bytes_per_sample)

    async def make_request(self, session: aiohttp.ClientSession, request_id: str) -> RequestMetrics:
        """Make a single request and measure metrics."""
        metrics = RequestMetrics(request_id=request_id, start_time=time.time())

        try:
            text = self.generate_random_text()

            # Prepare form data
            form_data = aiohttp.FormData()
            form_data.add_field("text", text)
            form_data.add_field("streaming", "true")

            # Make streaming request
            async with session.post(
                f"{self.base_url}/generate", data=form_data, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    metrics.error_message = f"HTTP {response.status}: {await response.text()}"
                    return metrics

                # Read streaming response
                audio_chunks = []
                first_chunk_received = False

                # Use iter_any() instead of iter_chunked() to properly detect end of stream
                async for chunk in response.content.iter_any():
                    if not chunk:  # Empty chunk indicates end of stream
                        break

                    current_time = time.time()

                    if not first_chunk_received:
                        metrics.ttfa = current_time - metrics.start_time
                        first_chunk_received = True

                    audio_chunks.append(chunk)

                # Calculate final metrics
                metrics.end_time = time.time()
                metrics.total_latency = metrics.end_time - metrics.start_time

                # Combine audio chunks and calculate duration
                full_audio = b"".join(audio_chunks)
                metrics.audio_duration = self.get_audio_duration(full_audio)

                # Calculate RTF (Real-Time Factor)
                if metrics.audio_duration and metrics.total_latency:
                    metrics.rtf = metrics.audio_duration / metrics.total_latency

                metrics.success = True

        except asyncio.TimeoutError:
            metrics.error_message = "Request timeout"
        except Exception as e:
            metrics.error_message = str(e)
        finally:
            if not metrics.end_time:
                metrics.end_time = time.time()
                metrics.total_latency = metrics.end_time - metrics.start_time

        return metrics

    async def run_benchmark(self, rate: float, duration: float) -> BenchmarkResults:
        """Run benchmark with specified request rate for given duration."""
        print(f"Starting benchmark: {rate} req/s for {duration}s")
        print(f"Target server: {self.base_url}")
        print("=" * 60)

        # Setup Poisson arrival process
        # For Poisson process, inter-arrival times follow exponential distribution
        # with parameter lambda = rate (average rate)
        end_time = time.time() + duration
        request_count = 0
        next_request_time = time.time()

        # Create HTTP session
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=50)
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            tasks = []

            # Schedule requests using Poisson process
            while next_request_time < end_time:
                # Wait until it's time for the next request
                current_time = time.time()
                if next_request_time > current_time:
                    await asyncio.sleep(next_request_time - current_time)

                request_count += 1
                request_id = f"req_{request_count:06d}"

                # Create request task
                task = asyncio.create_task(self.make_request(session, request_id))
                tasks.append(task)

                # Generate next inter-arrival time using exponential distribution
                # Mean inter-arrival time = 1/rate
                inter_arrival_time = np.random.exponential(1.0 / rate) if rate > 0 else float('inf')
                next_request_time += inter_arrival_time

            print(f"Scheduled {len(tasks)} requests. Waiting for completion...")

            # Wait for all requests to complete
            completed_metrics = await asyncio.gather(*tasks, return_exceptions=True)

            # Filter out exceptions and collect metrics
            for result in completed_metrics:
                if isinstance(result, RequestMetrics):
                    self.metrics.append(result)

                    # Print real-time progress
                    status = "✓" if result.success else "✗"
                    ttfa_str = f"{result.ttfa:.3f}s" if result.ttfa else "N/A"
                    rtf_str = f"{result.rtf:.3f}" if result.rtf else "N/A"
                    print(f"{status} {result.request_id}: TTFA={ttfa_str}, RTF={rtf_str}")

        return self.calculate_results()

    def calculate_results(self) -> BenchmarkResults:
        """Calculate aggregated benchmark results."""
        results = BenchmarkResults()

        if not self.metrics:
            return results

        results.total_requests = len(self.metrics)
        successful_metrics = [m for m in self.metrics if m.success]
        results.successful_requests = len(successful_metrics)
        results.failed_requests = results.total_requests - results.successful_requests

        if not successful_metrics:
            return results

        # Extract metrics for successful requests
        ttfa_values = [m.ttfa for m in successful_metrics if m.ttfa is not None]
        rtf_values = [m.rtf for m in successful_metrics if m.rtf is not None]
        latency_values = [m.total_latency for m in successful_metrics if m.total_latency is not None]

        # Calculate TTFA statistics
        if ttfa_values:
            ttfa_sorted = sorted(ttfa_values)
            results.ttfa_mean = statistics.mean(ttfa_values)
            results.ttfa_p50 = self._percentile(ttfa_sorted, 50)
            results.ttfa_p90 = self._percentile(ttfa_sorted, 90)
            results.ttfa_p95 = self._percentile(ttfa_sorted, 95)
            results.ttfa_p99 = self._percentile(ttfa_sorted, 99)
            results.ttfa_min = min(ttfa_values)
            results.ttfa_max = max(ttfa_values)

        # Calculate RTF statistics
        if rtf_values:
            rtf_sorted = sorted(rtf_values)
            results.rtf_mean = statistics.mean(rtf_values)
            results.rtf_p50 = self._percentile(rtf_sorted, 50)
            results.rtf_p90 = self._percentile(rtf_sorted, 90)
            results.rtf_p95 = self._percentile(rtf_sorted, 95)
            results.rtf_p99 = self._percentile(rtf_sorted, 99)
            results.rtf_min = min(rtf_values)
            results.rtf_max = max(rtf_values)

        # Calculate latency statistics
        if latency_values:
            latency_sorted = sorted(latency_values)
            results.latency_mean = statistics.mean(latency_values)
            results.latency_p50 = self._percentile(latency_sorted, 50)
            results.latency_p90 = self._percentile(latency_sorted, 90)
            results.latency_p95 = self._percentile(latency_sorted, 95)
            results.latency_p99 = self._percentile(latency_sorted, 99)
            results.latency_min = min(latency_values)
            results.latency_max = max(latency_values)

        return results

    def _percentile(self, sorted_values: List[float], percentile: int) -> float:
        """Calculate percentile from sorted values."""
        if not sorted_values:
            return 0.0

        index = (percentile / 100.0) * (len(sorted_values) - 1)
        lower_index = int(index)
        upper_index = min(lower_index + 1, len(sorted_values) - 1)

        if lower_index == upper_index:
            return sorted_values[lower_index]

        # Linear interpolation
        weight = index - lower_index
        return sorted_values[lower_index] * (1 - weight) + sorted_values[upper_index] * weight

    def print_results(self, results: BenchmarkResults):
        """Print formatted benchmark results."""
        print("\n" + "=" * 60)
        print("BENCHMARK RESULTS")
        print("=" * 60)

        # Request summary
        success_rate = (results.successful_requests / results.total_requests * 100) if results.total_requests > 0 else 0
        print(f"Total Requests:      {results.total_requests}")
        print(f"Successful:          {results.successful_requests}")
        print(f"Failed:              {results.failed_requests}")
        print(f"Success Rate:        {success_rate:.1f}%")
        print()

        if results.successful_requests == 0:
            print("No successful requests to analyze.")
            return

        # TTFA metrics
        print("TIME TO FIRST AUDIO (TTFA)")
        print("-" * 30)
        print(f"Mean:     {results.ttfa_mean:.3f}s")
        print(f"P50:      {results.ttfa_p50:.3f}s")
        print(f"P90:      {results.ttfa_p90:.3f}s")
        print(f"P95:      {results.ttfa_p95:.3f}s")
        print(f"P99:      {results.ttfa_p99:.3f}s")
        print(f"Min:      {results.ttfa_min:.3f}s")
        print(f"Max:      {results.ttfa_max:.3f}s")
        print()

        # RTF metrics
        print("REAL-TIME FACTOR (RTF)")
        print("-" * 30)
        print(f"Mean:     {results.rtf_mean:.3f}")
        print(f"P50:      {results.rtf_p50:.3f}")
        print(f"P90:      {results.rtf_p90:.3f}")
        print(f"P95:      {results.rtf_p95:.3f}")
        print(f"P99:      {results.rtf_p99:.3f}")
        print(f"Min:      {results.rtf_min:.3f}")
        print(f"Max:      {results.rtf_max:.3f}")
        print()

        # Total latency metrics
        print("TOTAL LATENCY")
        print("-" * 30)
        print(f"Mean:     {results.latency_mean:.3f}s")
        print(f"P50:      {results.latency_p50:.3f}s")
        print(f"P90:      {results.latency_p90:.3f}s")
        print(f"P95:      {results.latency_p95:.3f}s")
        print(f"P99:      {results.latency_p99:.3f}s")
        print(f"Min:      {results.latency_min:.3f}s")
        print(f"Max:      {results.latency_max:.3f}s")
        print()

        # Performance insights
        print("PERFORMANCE INSIGHTS")
        print("-" * 30)
        avg_rtf = results.rtf_mean
        if avg_rtf > 1.0:
            print(f"⚡ System is {avg_rtf:.1f}x FASTER than real-time")
        elif avg_rtf < 1.0:
            print(f"⚠️  System is {1 / avg_rtf:.1f}x SLOWER than real-time")
        else:
            print("📊 System runs at exactly real-time speed")

        if results.ttfa_p95 < 0.5:
            print("✅ Excellent TTFA latency (P95 < 0.5s)")
        elif results.ttfa_p95 < 1.0:
            print("👍 Good TTFA latency (P95 < 1.0s)")
        else:
            print("⚠️  High TTFA latency (P95 > 1.0s)")


async def main():
    parser = argparse.ArgumentParser(description="Benchmark vox-serve TTS server")
    parser.add_argument("--host", default="localhost", help="Server host (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
    parser.add_argument("--rate", type=float, default=1.0, help="Request rate (req/s, default: 1.0)")
    parser.add_argument("--duration", type=float, default=10.0, help="Test duration (seconds, default: 10.0)")

    args = parser.parse_args()

    # Validate arguments
    if args.rate <= 0:
        print("Error: Request rate must be positive")
        return 1

    if args.duration <= 0:
        print("Error: Duration must be positive")
        return 1

    # Create and run benchmark
    client = BenchmarkClient(args.host, args.port)

    try:
        results = await client.run_benchmark(args.rate, args.duration)
        client.print_results(results)
        return 0
    except KeyboardInterrupt:
        print("\nBenchmark interrupted by user")
        if client.metrics:
            print("Calculating results for completed requests...")
            results = client.calculate_results()
            client.print_results(results)
        return 1
    except Exception as e:
        print(f"Error running benchmark: {e}")
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
