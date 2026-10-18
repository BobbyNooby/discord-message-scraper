from .cli import main

if __name__ == "__main__":
    # The return code is the exit status: a bad subcommand must not look like success.
    raise SystemExit(main())
