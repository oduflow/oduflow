import re

from oduflow import prod_tune


def _parse(conf: str) -> dict[str, str]:
    """Parse a postgresql.conf body into {key: value}, ignoring comments/blanks."""
    out: dict[str, str] = {}
    for line in conf.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def _mb(value: str) -> int:
    m = re.fullmatch(r"(\d+)(MB|GB|kB)", value)
    assert m, f"unexpected memory value: {value!r}"
    n, unit = int(m.group(1)), m.group(2)
    return {"kB": n // 1024, "MB": n, "GB": n * 1024}[unit]


class TestProdPostgresConf:
    def test_first_line_is_keep_marker(self):
        conf = prod_tune.generate_prod_postgresql_conf(8192, 4)
        assert conf.splitlines()[0] == "# KEEP"
        assert conf.splitlines()[1].startswith("# ODUFLOW-TUNE ")

    def test_archiving_retains_wal_until_provisioning_decides(self):
        # archive_mode needs a PG restart to toggle, so it must ship enabled;
        # the actual command is flipped later via ALTER SYSTEM by walg.py.
        settings = _parse(prod_tune.generate_prod_postgresql_conf(8192, 4))
        assert settings["archive_mode"] == "on"
        assert settings["archive_command"] == "''"
        assert settings["wal_level"] == "replica"

    def test_shared_buffers_share_of_ram(self):
        settings = _parse(prod_tune.generate_prod_postgresql_conf(16384, 8))
        assert _mb(settings["shared_buffers"]) == round(16384 * 0.20)

    def test_shared_buffers_floor_and_cap(self):
        small = _parse(prod_tune.generate_prod_postgresql_conf(1024, 2))
        assert _mb(small["shared_buffers"]) == 512
        huge = _parse(prod_tune.generate_prod_postgresql_conf(128 * 1024, 32))
        assert _mb(huge["shared_buffers"]) == 8192

    def test_max_connections_default(self):
        settings = _parse(prod_tune.generate_prod_postgresql_conf(8192, 4))
        assert settings["max_connections"] == "200"

    def test_full_memory_profile_for_a_16gb_8cpu_host(self):
        # One reference host pinned end to end. Derivations: shared_buffers
        # 20% of 16384; effective_cache 45%; work_mem sb//(mc//4) capped at 64;
        # maintenance sb//4; wal_buffers 3% of sb capped at 64.
        settings = _parse(prod_tune.generate_prod_postgresql_conf(16384, 8))
        assert _mb(settings["shared_buffers"]) == 3277
        assert _mb(settings["effective_cache_size"]) == 7373
        assert _mb(settings["work_mem"]) == 64
        assert _mb(settings["maintenance_work_mem"]) == 819
        assert _mb(settings["wal_buffers"]) == 64

    def test_full_memory_profile_for_a_small_host(self):
        # 2 GB: every value lands on its floor except the derived ones.
        settings = _parse(prod_tune.generate_prod_postgresql_conf(2048, 2))
        assert _mb(settings["shared_buffers"]) == 512
        assert _mb(settings["effective_cache_size"]) == 1024
        assert _mb(settings["work_mem"]) == 10
        assert _mb(settings["maintenance_work_mem"]) == 128
        assert _mb(settings["wal_buffers"]) == 15

    def test_effective_cache_floor_and_cap(self):
        assert (
            _mb(
                _parse(prod_tune.generate_prod_postgresql_conf(1024, 2))[
                    "effective_cache_size"
                ]
            )
            == 1024
        )
        assert (
            _mb(
                _parse(prod_tune.generate_prod_postgresql_conf(512 * 1024, 32))[
                    "effective_cache_size"
                ]
            )
            == 65536
        )

    def test_work_mem_floor_and_cap(self):
        # Floor 8 MB with a tiny shared_buffers, cap 64 MB with a large one.
        tiny = _parse(
            prod_tune.generate_prod_postgresql_conf(1024, 2, max_connections=4000)
        )
        assert _mb(tiny["work_mem"]) == 8
        big = _parse(prod_tune.generate_prod_postgresql_conf(128 * 1024, 32))
        assert _mb(big["work_mem"]) == 64

    def test_maintenance_work_mem_floor_and_cap(self):
        small = _parse(prod_tune.generate_prod_postgresql_conf(1024, 2))
        assert _mb(small["maintenance_work_mem"]) == 128
        huge = _parse(prod_tune.generate_prod_postgresql_conf(128 * 1024, 32))
        assert _mb(huge["maintenance_work_mem"]) == 1024

    def test_wal_buffers_floor_and_cap(self):
        small = _parse(prod_tune.generate_prod_postgresql_conf(1024, 2))
        assert _mb(small["wal_buffers"]) == 15
        huge = _parse(prod_tune.generate_prod_postgresql_conf(128 * 1024, 32))
        assert _mb(huge["wal_buffers"]) == 64

    def test_parallel_gather_uses_the_production_cpu_budget(self):
        # The unified plan gives production PostgreSQL roughly half the host
        # CPUs, then applies the below-4 / 4-and-up parallelism formula.
        def gather(cpu):
            return _parse(prod_tune.generate_prod_postgresql_conf(16384, cpu))[
                "max_parallel_workers_per_gather"
            ]

        assert gather(1) == "1"
        assert gather(2) == "1"
        assert gather(3) == "2"
        assert gather(4) == "2"  # max(2, 2)
        assert gather(8) == "2"
        assert gather(12) == "3"
        assert gather(32) == "8"

    def test_parallel_maintenance_workers_are_capped_at_four(self):
        def maint(cpu):
            return _parse(prod_tune.generate_prod_postgresql_conf(16384, cpu))[
                "max_parallel_maintenance_workers"
            ]

        assert maint(1) == "1"
        assert maint(4) == "1"
        assert maint(8) == "2"
        assert maint(32) == "4"  # capped

    def test_autovacuum_workers_are_clamped_between_three_and_six(self):
        def workers(cpu):
            return _parse(prod_tune.generate_prod_postgresql_conf(16384, cpu))[
                "autovacuum_max_workers"
            ]

        assert workers(1) == "3"  # floor
        assert workers(8) == "3"
        assert workers(16) == "4"
        assert workers(24) == "6"
        assert workers(32) == "6"  # cap

    def test_worker_processes_track_the_production_cpu_budget(self):
        settings = _parse(prod_tune.generate_prod_postgresql_conf(16384, 8))
        assert settings["max_worker_processes"] == "4"
        assert settings["max_parallel_workers"] == "4"

    def test_degenerate_resources_still_produce_a_valid_conf(self):
        # Detection can fail and report zeroes; the file must still be usable.
        settings = _parse(prod_tune.generate_prod_postgresql_conf(0, 0))
        assert _mb(settings["shared_buffers"]) == 512
        assert settings["max_worker_processes"] == "1"
        assert settings["max_parallel_maintenance_workers"] == "1"

    def test_max_connections_floor(self):
        settings = _parse(
            prod_tune.generate_prod_postgresql_conf(8192, 4, max_connections=0)
        )
        assert settings["max_connections"] == "1"

    def test_header_reports_detected_resources_and_version(self):
        conf = prod_tune.generate_prod_postgresql_conf(
            8192, 4, source="docker", oduflow_version="9.9.9"
        )
        assert "4 vCPU, 8192 MB RAM (source: docker)" in conf
        assert "9.9.9" in conf


class TestOdooWorkerSettings:
    def test_standard_formula(self):
        # Unified plan gives Odoo 75% of 4 CPU: workers = 2*3+1 = 7.
        opts = prod_tune.compute_odoo_worker_settings(4, 32768)
        assert opts["workers"] == "7"
        assert opts["max_cron_threads"] == "1"
        assert opts["proxy_mode"] == "True"
        assert opts["list_db"] == "False"

    def test_ram_bounded(self):
        # 4 GB host: 45% RAM budget = ~1.8GB -> 2 workers minimum floor.
        opts = prod_tune.compute_odoo_worker_settings(8, 4096)
        assert opts["workers"] == "2"

    def test_cap_override(self):
        opts = prod_tune.compute_odoo_worker_settings(16, 128 * 1024, workers_cap=20)
        assert opts["workers"] == "20"

    def test_db_maxconn_scales_with_workers(self):
        # Unified plan gives this host 7 workers -> 2*7+3 = 17 connections.
        opts = prod_tune.compute_odoo_worker_settings(4, 32768)
        assert opts["workers"] == "7"
        assert opts["db_maxconn"] == "17"

    def test_db_maxconn_has_a_floor_of_16(self):
        # 2 workers -> 2*2+3 = 7, raised to the 16-connection floor.
        opts = prod_tune.compute_odoo_worker_settings(1, 2048)
        assert opts["workers"] == "2"
        assert opts["db_maxconn"] == "16"

    def test_minimum_two_workers(self):
        opts = prod_tune.compute_odoo_worker_settings(1, 2048)
        assert int(opts["workers"]) >= 2

    def test_workers_follow_two_cpu_plus_one_when_unconstrained(self):
        # 2 CPU, 8 GB: 2*2+1 = 5 wanted, RAM budget allows 3 (8192*0.45//1024).
        assert prod_tune.compute_odoo_worker_settings(2, 8192)["workers"] == "3"

    def test_memory_limits_are_explicit_odoo_defaults_in_bytes(self):
        opts = prod_tune.compute_odoo_worker_settings(4, 32768)
        assert opts["limit_memory_soft"] == str(2048 * 1024 * 1024)
        assert opts["limit_memory_hard"] == str(2560 * 1024 * 1024)
        assert opts["limit_time_cpu"] == "600"
        assert opts["limit_time_real"] == "1200"
        assert opts["limit_request"] == "65536"

    def test_ram_budget_has_a_floor_of_two_workers(self):
        # Even a host with no detectable RAM must not compute zero workers.
        assert prod_tune.compute_odoo_worker_settings(8, 0)["workers"] == "2"

    def test_single_cpu_host_uses_the_formula_not_a_floor(self):
        # 1 CPU with plenty of RAM: 2*1+1 = 3 workers, nothing binding.
        # Pins both the CPU floor (1, not 2) and the 2*cpu+1 coefficients.
        assert prod_tune.compute_odoo_worker_settings(1, 32768)["workers"] == "3"

    def test_ram_budget_is_one_worker_per_gib_of_the_45_percent_share(self):
        # 6827 MB * 0.45 = 3072 MB -> exactly 3 GiB -> 3 workers, below both
        # the planned-CPU formula (13) and the cap (8).
        assert prod_tune.compute_odoo_worker_settings(8, 6827)["workers"] == "3"
