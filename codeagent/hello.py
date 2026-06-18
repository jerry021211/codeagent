"""A simple greeting module."""


def greet(name: str) -> None:
    """Print a personalized greeting message.

    Args:
        name: The name of the person to greet.
    """
    message = "Hello, " + name
    print(message)


if __name__ == "__main__":
    greet("Claude")