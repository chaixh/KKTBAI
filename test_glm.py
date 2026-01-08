import asyncio
import httpx

# 替换为你的API Key
API_KEY = "92a8517f902a4489bbac76c77f5c4ead.RcnhkzoV6ykoOgfU"
API_BASE = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-4-free"


async def test_glm_api():
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"glm-key {API_KEY}"  # 核心：glm-key前缀
    }
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "你好，测试一下"}]
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(API_BASE, json=payload, headers=headers)
        print(f"响应状态码：{response.status_code}")
        print(f"响应内容：{response.text}")


if __name__ == "__main__":
    asyncio.run(test_glm_api())