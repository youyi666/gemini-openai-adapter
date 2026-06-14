def get_features():
    return [
        "health",
        "models",
        "chat_completions",
        "usage",
    ]


def format_status(name, enabled=True):
    if enabled:
        return f"{name}: enabled"
    return f"{name}: disabled"


def main():
    features = get_features()
    for feature in features:
        print(format_status(feature))


if __name__ == "__main__":
    main()