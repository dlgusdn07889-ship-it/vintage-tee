def score_item(title, price):
    score = 0

    title = title.lower()

    # 희귀 태그
    rare_tags = [
        "giant",
        "brockum",
        "winterland",
        "changes",
        "anvil",
        "all sport",
        "screen stars",
        "oneita",
        "stedman",
        "tee jays"
    ]

    for tag in rare_tags:
        if tag in title:
            score += 20

    # Made in USA
    if "usa" in title:
        score += 10

    # Single Stitch
    if "single stitch" in title:
        score += 15

    # 가격이 싸면 가산점
    if price < 50:
        score += 25
    elif price < 100:
        score += 15
    elif price < 150:
        score += 5

    return score


if __name__ == "__main__":
    title = "1994 Nirvana Giant Single Stitch USA"
    price = 60

    print("Score:", score_item(title, price))
