from module import average


def summarize(values: list[float]) -> float:
    return average(values)  # type: ignore[no-any-return]
