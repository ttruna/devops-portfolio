#!/usr/bin/env python3
"""Монітор доступності API з класифікацією типів збоїв."""

import argparse
import errno
import json
import logging
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NETWORK_FAILURES = {"DNS", "З'єднання відхилено", "Тайм-аут", "Помилка сертифіката"}


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Розрізняє мережеві збої за етапом зупинки запиту."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc

    if isinstance(reason, ssl.SSLError):
        return "Помилка сертифіката", f"SSL/TLS помилка перевірки сертифіката: {reason}"

    if isinstance(reason, socket.gaierror):
        return "DNS", f"Не вдалося розв'язати доменне ім'я: {reason}"

    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "Тайм-аут", "Час очікування відповіді від сервера вичерпано"

    if isinstance(reason, ConnectionRefusedError):
        return "З'єднання відхилено", "Сервер відхилив з'єднання (порт закритий)"

    if isinstance(reason, OSError):
        if reason.errno == errno.ECONNREFUSED:
            return "З'єднання відхилено", "Сервер відхилив з'єднання (ECONNREFUSED)"
        if reason.errno in (errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH):
            return "Тайм-аут", f"Мережевий тайм-аут або вузол недосяжний: {reason}"
        if "timed out" in str(reason).lower():
            return "Тайм-аут", f"Тайм-аут сокета: {reason}"

    msg = str(reason).lower()
    if "certificate" in msg or "ssl" in msg:
        return "Помилка сертифіката", f"Помилка сертифіката: {reason}"
    if "name or service not known" in msg or "nodename nor servname" in msg or "getaddrinfo" in msg:
        return "DNS", f"Помилка DNS: {reason}"
    if "connection refused" in msg:
        return "З'єднання відхилено", f"З'єднання відхилено: {reason}"
    if "timed out" in msg:
        return "Тайм-аут", f"Тайм-аут запиту: {reason}"

    return "Мережева помилка", f"Інша помилка з'єднання: {reason}"


def single_probe(target: dict[str, Any]) -> dict[str, Any]:
    """Виконує один HTTP(S)-запит до цілі та повертає результат."""
    url = target["url"]
    expected_status = int(target.get("expected_status", 200))
    timeout = float(target.get("timeout", 5.0))
    threshold_ms = float(target.get("latency_threshold_ms", 1000.0))

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "DevOps-Lab03-APIMonitor/1.0"},
        method="GET",
    )
    ssl_ctx = ssl.create_default_context()

    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            status_code = resp.getcode()
            resp.read(256)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

        if status_code != expected_status:
            return {
                "healthy": False,
                "status_code": status_code,
                "response_time_ms": elapsed_ms,
                "failure_type": "Несподіваний код",
                "details": f"Очікувався код {expected_status}, отримано {status_code}",
            }

        if elapsed_ms > threshold_ms:
            return {
                "healthy": False,
                "status_code": status_code,
                "response_time_ms": elapsed_ms,
                "failure_type": "Повільна відповідь",
                "details": f"Час відповіді {elapsed_ms} мс перевищив поріг {threshold_ms} мс",
            }

        return {
            "healthy": True,
            "status_code": status_code,
            "response_time_ms": elapsed_ms,
            "failure_type": None,
            "details": "OK",
        }

    except urllib.error.HTTPError as http_err:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        if http_err.code != expected_status:
            return {
                "healthy": False,
                "status_code": http_err.code,
                "response_time_ms": elapsed_ms,
                "failure_type": "Несподіваний код",
                "details": f"Очікувався код {expected_status}, отримано {http_err.code}",
            }
        if elapsed_ms > threshold_ms:
            return {
                "healthy": False,
                "status_code": http_err.code,
                "response_time_ms": elapsed_ms,
                "failure_type": "Повільна відповідь",
                "details": f"Час відповіді {elapsed_ms} мс перевищив поріг {threshold_ms} мс",
            }
        return {
            "healthy": True,
            "status_code": http_err.code,
            "response_time_ms": elapsed_ms,
            "failure_type": None,
            "details": "OK",
        }

    except Exception as exc:  # noqa: BLE001
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        failure_type, details = classify_exception(exc)
        return {
            "healthy": False,
            "status_code": None,
            "response_time_ms": elapsed_ms,
            "failure_type": failure_type,
            "details": details,
        }


def check_target_with_retry(target: dict[str, Any], logger: logging.Logger) -> dict[str, Any]:
    """Перевіряє ціль; при мережевій помилці виконує 1 повторну спробу."""
    result = single_probe(target)
    attempts = 1

    if not result["healthy"] and result["failure_type"] in NETWORK_FAILURES:
        logger.info(
            "Ціль '%s' (%s): мережева помилка [%s], виконую повторну спробу...",
            target["name"],
            target["url"],
            result["failure_type"],
        )
        time.sleep(0.5)
        result = single_probe(target)
        attempts = 2

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "name": target["name"],
        "url": target["url"],
        "status": "HEALTHY" if result["healthy"] else "UNHEALTHY",
        "http_status": result["status_code"],
        "response_time_ms": result["response_time_ms"],
        "failure_type": result["failure_type"],
        "details": result["details"],
        "attempts": attempts,
    }


def send_webhook_alert(webhook_url: str, record: dict[str, Any], logger: logging.Logger) -> None:
    """Надсилає сповіщення про збій у Slack/Discord через вебхук."""
    text = (
        f"[API ALERT] Ціль: {record['name']} ({record['url']})\n"
        f"Тип збою: {record['failure_type']}\n"
        f"Деталі: {record['details']} (час: {record['response_time_ms']} мс)"
    )
    payload = json.dumps({"text": text, "content": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DevOps-Lab03-APIMonitor/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0):
            logger.info("Сповіщення надіслано у вебхук для '%s'.", record["name"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не вдалося надіслати сповіщення у вебхук: %s", exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Монітор доступності API з розрізненням типів збоїв.",
        epilog="Приклад: python3 api_monitor.py --config config/targets.json --output monitor_results.json",
    )
    parser.add_argument("--config", required=True, help="Шлях до JSON-файлу конфігурації зі списком цілей.")
    parser.add_argument("--output", default="monitor_results.json", help="Шлях до JSON-файлу результатів.")
    parser.add_argument("--log-file", default="api_monitor.log", help="Шлях до файлу журналу.")
    return parser.parse_args()


def setup_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("api_monitor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)

    if not config_path.is_file():
        print(f"Помилка: файл конфігурації '{config_path}' не знайдено.", file=sys.stderr)
        return 2

    try:
        with config_path.open("r", encoding="utf-8") as f:
            config_data = json.load(f)
    except json.JSONDecodeError as exc:
        print(f"Помилка: некоректний JSON у файлі '{config_path}': {exc}", file=sys.stderr)
        return 2

    targets = config_data.get("targets")
    if not isinstance(targets, list) or not targets:
        print("Помилка: у конфігурації відсутній непорожній список 'targets'.", file=sys.stderr)
        return 2

    logger = setup_logger(args.log_file)
    webhook_url = os.environ.get("MONITOR_WEBHOOK_URL", "").strip()

    logger.info("Запуск перевірки %d цілей із конфігурації '%s'", len(targets), config_path)

    results: list[dict[str, Any]] = []
    has_failures = False

    for target in targets:
        if "url" not in target or "name" not in target:
            print("Помилка: кожна ціль у конфігурації повинна мати 'name' та 'url'.", file=sys.stderr)
            return 2

        record = check_target_with_retry(target, logger)
        results.append(record)

        if record["status"] == "HEALTHY":
            logger.info(
                "УСПІХ | %s (%s) | код=%s | час=%s мс",
                record["name"],
                record["url"],
                record["http_status"],
                record["response_time_ms"],
            )
        else:
            has_failures = True
            logger.error(
                "ЗБІЙ [%s] | %s (%s) | %s | час=%s мс",
                record["failure_type"],
                record["name"],
                record["url"],
                record["details"],
                record["response_time_ms"],
            )
            if webhook_url:
                send_webhook_alert(webhook_url, record, logger)

    output_path = Path(args.output)
    with output_path.open("w", encoding="utf-8") as out_f:
        json.dump(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "total": len(results),
                "unhealthy": sum(1 for r in results if r["status"] != "HEALTHY"),
                "results": results,
            },
            out_f,
            ensure_ascii=False,
            indent=2,
        )

    logger.info("Результати збережено у '%s'. Завершення перевірки.", output_path)
    return 1 if has_failures else 0


if __name__ == "__main__":
    sys.exit(main())
