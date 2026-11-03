def main() -> None:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "benchmark":
        from evebench.benchmark.__main__ import main as benchmark_main

        benchmark_main(sys.argv[2:])
        return

    from evebench.observability import configure_logging

    configure_logging()
    import uvicorn

    uvicorn.run("evebench.api:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
