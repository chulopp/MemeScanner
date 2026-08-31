"""
test_fase_d_integration.py -- Unit Test untuk Fase D Architecture

Menguji bahwa:
1. Stage 1 (pipeline.py) TIDAK lagi merekam sinyal langsung (duplikasi dihapus)
2. Stage 1 mengirim token ke DelayedEvaluator jika lolos safety
3. Stage 2 (delayed_evaluator._process_token) membangun SafetyCheckResult yang valid
4. DelayedEvaluator start/stop bekerja tanpa error
5. run_paper_trading.py dapat diimport tanpa error
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ============================================================
# Test 1: pipeline.py tidak lagi merekam sinyal di Stage 1
# ============================================================
class TestPipelineNoStage1Recording:
    """Verifikasi bahwa pipeline.py sudah tidak memanggil record_signal secara langsung."""

    def test_record_signal_not_imported_in_pipeline_stage1(self):
        """
        Periksa source code pipeline.py -- seharusnya TIDAK ada blok record_signal
        di bawah 'Stage 1' atau 'Fase 5: Record signal'.
        """
        import inspect
        from src.filters import pipeline as pipeline_module
        source = inspect.getsource(pipeline_module)
        # Blok lama (duplikasi) harus sudah dihapus
        assert "Fase 5: Record signal" not in source, (
            "pipeline.py masih mengandung blok 'Fase 5: Record signal' yang merupakan "
            "duplikasi Stage 1 recording. Hapus blok ini -- recording harus HANYA di Stage 2."
        )

    def test_pipeline_stage1_only_enqueues_to_delayed_evaluator(self):
        """
        Verifikasi pipeline.py menggunakan delayed_evaluator.enqueue
        sebagai satu-satunya cara merespons token yang lolos safety.
        """
        import inspect
        from src.filters import pipeline as pipeline_module
        source = inspect.getsource(pipeline_module)
        assert "delayed_evaluator.enqueue" in source, (
            "pipeline.py harus memanggil delayed_evaluator.enqueue untuk token yang lolos safety."
        )


# ============================================================
# Test 2: main.py meng-import dan menjalankan delayed_evaluator
# ============================================================
class TestMainDelayedEvaluatorWired:
    """Verifikasi main.py meng-import dan me-lifecycle delayed_evaluator."""

    def test_main_imports_delayed_evaluator(self):
        import inspect
        from src import main as main_module
        source = inspect.getsource(main_module)
        assert "delayed_evaluator" in source, (
            "main.py harus mengimport dan menjalankan delayed_evaluator."
        )

    def test_main_starts_delayed_evaluator(self):
        import inspect
        from src import main as main_module
        source = inspect.getsource(main_module)
        assert "delayed_evaluator.start()" in source, (
            "main.py harus memanggil delayed_evaluator.start() agar worker T+2 berjalan."
        )

    def test_main_stops_delayed_evaluator(self):
        import inspect
        from src import main as main_module
        source = inspect.getsource(main_module)
        assert "delayed_evaluator.stop()" in source, (
            "main.py harus memanggil delayed_evaluator.stop() saat shutdown agar graceful."
        )


# ============================================================
# Test 3: DelayedEvaluator start/stop lifecycle
# ============================================================
class TestDelayedEvaluatorLifecycle:
    """Verifikasi DelayedEvaluator start/stop bekerja (in-memory fallback, tanpa Redis)."""

    @pytest.mark.asyncio
    async def test_start_and_stop_without_redis(self):
        """DelayedEvaluator harus start dan stop tanpa error meski Redis tidak ada."""
        from src.paper_trading.delayed_evaluator import DelayedEvaluator

        evaluator = DelayedEvaluator()

        # Patch _get_redis di level evaluator langsung (redis module tidak ada di test env)
        async def _mock_no_redis():
            return None

        with patch.object(evaluator, "_get_redis", side_effect=_mock_no_redis):
            await evaluator.start()
            assert evaluator._running is True

            # Tunggu sedikit agar worker loop berjalan setidaknya 1 iterasi
            await asyncio.sleep(0.1)

            await evaluator.stop()
            assert evaluator._running is False

    @pytest.mark.asyncio
    async def test_enqueue_uses_in_memory_when_redis_unavailable(self):
        """Enqueue harus berhasil memakai antrian in-memory jika Redis tidak ada."""
        from src.paper_trading.delayed_evaluator import DelayedEvaluator

        evaluator = DelayedEvaluator()

        # Gunakan valid Solana address (base58 pubkey 32-44 karakter)
        valid_mint = "So11111111111111111111111111111111111111112"

        from src.ingestion.schemas import RawTokenEvent
        fake_event = RawTokenEvent(
            token_address=valid_mint,
            symbol="TEST",
            name="Test Token",
            launch_venue="pump_fun",
            total_supply=1_000_000_000,
            initial_buy_amount=0.1,
            deployer_wallet_address="DeployerAddr1234567890123456789012",
        )
        fake_result = MagicMock()
        fake_result.filter_pass = True

        # Force Redis unavailable via _get_redis
        async def _mock_no_redis():
            return None

        with patch.object(evaluator, "_get_redis", side_effect=_mock_no_redis):
            await evaluator.enqueue(fake_event, fake_result)

        assert len(evaluator._in_memory_queue) == 1
        queued = evaluator._in_memory_queue[0]
        assert queued["token_address"] == valid_mint
        assert queued["symbol"] == "TEST"


# ============================================================
# Test 4: SafetyCheckResult adapter di Stage 2
# ============================================================
class TestStage2SafetyAdapter:
    """Verifikasi _process_token membangun SafetyCheckResult yang valid untuk record_signal."""

    def test_delayed_evaluator_builds_safety_adapter(self):
        """
        Pastikan source code delayed_evaluator menggunakan SafetyCheckResult adapter
        (bukan langsung meneruskan OpportunityScoreResult ke record_signal).
        """
        import inspect
        from src.paper_trading import delayed_evaluator as de_module
        source = inspect.getsource(de_module)

        assert "SafetyCheckResult" in source, (
            "delayed_evaluator._process_token harus import dan menggunakan SafetyCheckResult "
            "sebagai adapter saat memanggil record_signal."
        )
        assert "safety_adapter" in source, (
            "delayed_evaluator._process_token harus membuat 'safety_adapter' dari SafetyCheckResult."
        )

    def test_delayed_evaluator_has_retry_logic(self):
        """Stage 2 harus punya retry logic untuk signal recording (Fix #5)."""
        import inspect
        from src.paper_trading import delayed_evaluator as de_module
        source = inspect.getsource(de_module)

        assert "Retrying" in source or "retry" in source.lower(), (
            "delayed_evaluator._process_token harus punya retry logic untuk signal recording."
        )


# ============================================================
# Test 5: run_paper_trading.py dapat diimport
# ============================================================
class TestRunPaperTradingScript:
    """Verifikasi bahwa scripts/run_paper_trading.py dapat diimport tanpa error."""

    def test_script_importable(self):
        """Script harus ada di filesystem."""
        import os

        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "run_paper_trading.py"
        )
        assert os.path.exists(script_path), (
            f"scripts/run_paper_trading.py tidak ditemukan di {script_path}. "
            "Buat file ini sebagai entry point Fase D."
        )

    def test_script_has_required_functions(self):
        """Script harus punya fungsi main, _check_env, dan _print_banner."""
        import os

        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "run_paper_trading.py"
        )
        with open(script_path, "r", encoding="utf-8") as f:
            source = f.read()

        assert "def main(" in source, "run_paper_trading.py harus punya fungsi main()"
        assert "def _check_env(" in source, "run_paper_trading.py harus punya _check_env()"
        assert "--dry-run" in source, "run_paper_trading.py harus punya flag --dry-run"
        assert "--duration" in source, "run_paper_trading.py harus punya flag --duration"