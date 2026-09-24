from agent_chat_ui.app import ConfigError, log_event, main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        log_event("configuration_error", error=str(exc))
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
