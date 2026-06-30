import openai


def generate_chapter(input_pack):
    response = openai.ChatComplation.create(
        model="gpt-4o-mini",
        message=[
            {"role": "system", "content": "你是专业末日小说编剧"},
            {"role": "user", "content": input_pack}
        ]
    )
    return response["choices"][0]["message"]["content"]
