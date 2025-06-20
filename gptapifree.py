# -*- coding: utf-8 -*-
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Union, Dict, Optional
import json, os, uuid, aiosqlite, httpx
from pathlib import Path
from fastapi.responses import StreamingResponse
import asyncio
from fastapi import Request

app = FastAPI()

# 配置文件上传目录
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# API配置
API_KEY = "sk-xbAnmbF8Dn2r6mPhOAZH7Q9gWNFtbJ3OUqmkMw8bkkIZaa4b"
BASE_URL = "https://yibuapi.com/v1"


# 数据模型
class TextContent(BaseModel):
    type: str = "text"
    text: str


class ImageUrlContent(BaseModel):
    type: str = "image_url"
    image_url: Dict[str, str]

class ImageGenRequest(BaseModel):
    prompt: str
    n: Optional[int] = 1         # 生成几张图，默认1张
    size: Optional[str] = "1024x1024"  # 图片尺寸

MessagePart = Union[TextContent, ImageUrlContent]


class ChatRequest(BaseModel):
    message: List[MessagePart]  # 保持原字段名不变
    session_id: str
    model_provider: str  # openai | claude | gemini


# 初始化数据库
@app.on_event("startup")
async def init_db():
    async with aiosqlite.connect("chat_history.db") as db:
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS messages
                         (
                             id
                             INTEGER
                             PRIMARY
                             KEY
                             AUTOINCREMENT,
                             session_id
                             TEXT,
                             role
                             TEXT,
                             content
                             TEXT,
                             created_at
                             TIMESTAMP
                             DEFAULT
                             CURRENT_TIMESTAMP
                         );
                         """)
        await db.commit()


async def async_chat_completion_stream(messages: List[Dict], provider: str):
    """流式处理不同模型的聊天请求"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    # 消息格式化逻辑与之前相同
    last_message = messages[-1]
    if isinstance(last_message["content"], list):
        content_parts = []
        for part in last_message["content"]:
            if part["type"] == "text":
                content_parts.append({"type": "text", "text": part["text"]})
            elif part["type"] == "image_url":
                image_url = part["image_url"]["url"]
                if not image_url.startswith(("http://", "https://")):
                    image_url = f"http://localhost:8000{image_url}"
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": image_url}
                })

        formatted_messages = messages[:-1] + [{
            "role": last_message["role"],
            "content": content_parts
        }]
    else:
        formatted_messages = messages

    try:
        if provider == "openai":
            json_data = {
                "model": "gpt-4o",
                "messages": formatted_messages,
                "max_tokens": 1024,
                "stream": True  # 启用流式
            }
        elif provider == "claude":
            json_data = {
                "model": "claude-3-5-haiku",
                "messages": formatted_messages,
                "max_tokens": 1024,
                "stream": True
            }
        elif provider == "gemini":
            json_data = {
                "model": "gemini-2.0-flash",
                "messages": formatted_messages,
                "stream": True
            }
        else:
            raise ValueError("不支持的模型类型")

        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                    "POST",
                    f"{BASE_URL}/chat/completions",
                    headers=headers,
                    json=json_data
            ) as response:
                response.raise_for_status()

                async for chunk in response.aiter_text():
                    if chunk.strip():
                        yield f"data: {chunk}\n\n"
                        await asyncio.sleep(0.01)  # 控制流速度

    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"

@app.post("/generate_image")
async def generate_image(request: ImageGenRequest):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    payload = {

        "model": "dall-e-3",  # 假设用dall-e-3模型
        "prompt": request.prompt,
        "n": request.n,
        "size": request.size,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            resp = await client.post(f"{BASE_URL}/images/generations", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            # data结构根据API返回调整，假设返回格式是 { "data": [ { "url": "..."} ] }
            urls = [item["url"] for item in data.get("data", [])]
            return {"urls": urls}
        except Exception as e:
            return {"error": str(e)}


@app.post("/chat_stream")
async def chat_stream(request: ChatRequest):
    """流式聊天接口"""
    try:
        # 1. 从数据库加载历史消息 (与普通接口相同)
        async with aiosqlite.connect("chat_history.db") as db:
            cursor = await db.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY created_at ASC LIMIT 20",
                (request.session_id,)
            )
            history = []
            for role, content in await cursor.fetchall():
                try:
                    content_obj = json.loads(content)
                except:
                    content_obj = content
                history.append({"role": role, "content": content_obj})

        # 2. 添加系统消息 (与普通接口相同)
        if not history or history[0]["role"] != "system":
            history.insert(0, {
                "role": "system",
                "content": "你是一个专业、耐心且善于沟通的AI助手。"
            })

        # 3. 添加用户新消息 (与普通接口相同)
        user_content = [part.dict() for part in request.message]
        history.append({"role": "user", "content": user_content})

        # 4. 保存用户消息到数据库
        async with aiosqlite.connect("chat_history.db") as db:
            await db.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (request.session_id, "user", json.dumps(user_content))
            )
            await db.commit()

        # 5. 返回流式响应
        return StreamingResponse(
            async_chat_completion_stream(history, request.model_provider.lower()),
            media_type="text/event-stream"
        )

    except Exception as e:
        async def error_stream():
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return StreamingResponse(
            error_stream(),
            media_type="text/event-stream",
            status_code=500
        )


# @app.post("/chat")
# async def chat(request: ChatRequest):
#     try:
#         # 1. 从数据库加载历史消息
#         async with aiosqlite.connect("chat_history.db") as db:
#             cursor = await db.execute(
#                 "SELECT role, content FROM messages WHERE session_id = ? ORDER BY created_at ASC LIMIT 20",
#                 (request.session_id,)
#             )
#             history = []
#             for role, content in await cursor.fetchall():
#                 try:
#                     content_obj = json.loads(content)
#                 except:
#                     content_obj = content
#                 history.append({"role": role, "content": content_obj})
#
#         # 2. 添加系统消息（如果不存在）
#         if not history or history[0]["role"] != "system":
#             history.insert(0, {
#                 "role": "system",
#                 "content": "你是一个专业、耐心且善于沟通的AI助手。"
#             })
#
#         # 3. 添加用户新消息（保持原结构）
#         user_content = [part.dict() for part in request.message]
#         history.append({"role": "user", "content": user_content})
#
#         # 4. 调用AI模型
#         response = await async_chat_completion_stream(history, request.model_provider.lower())
#
#         # 5. 保存到数据库
#         async with aiosqlite.connect("chat_history.db") as db:
#             await db.execute(
#                 "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
#                 (request.session_id, "user", json.dumps(user_content))
#             )
#             await db.execute(
#                 "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
#                 (request.session_id, "assistant", response["reply"])
#             )
#             await db.commit()
#
#         return response
#
#     except HTTPException as e:
#         return JSONResponse(
#             status_code=e.status_code,
#             content={"error": e.detail}
#         )
#     except Exception as e:
#         return JSONResponse(
#             status_code=500,
#             content={"error": f"Internal server error: {str(e)}"}
#         )


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    try:
        file_ext = Path(file.filename).suffix
        filename = f"{uuid.uuid4().hex}{file_ext}"
        filepath = os.path.join(UPLOAD_DIR, filename)

        with open(filepath, "wb") as f:
            content = await file.read()
            f.write(content)

        # 返回完整URL而不是相对路径
        return {
            "url": f"http://localhost:8000/uploads/{filename}",
            "relative_url": f"/uploads/{filename}"  # 同时保留相对路径
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件上传失败: {str(e)}")


async def async_chat_completion_stream(messages: List[Dict], provider: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    # 统一格式化消息
    formatted_messages = []
    for msg in messages:
        if isinstance(msg["content"], str):
            # 历史文本消息
            formatted_messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })
        else:
            # 处理多模态消息
            content_parts = []
            for part in msg["content"]:
                if part["type"] == "text":
                    content_parts.append({"type": "text", "text": part["text"]})
                elif part["type"] == "image_url":
                    image_url = part["image_url"]["url"]
                    # 确保使用完整URL
                    if not image_url.startswith(("http://", "https://")):
                        image_url = f"http://localhost:8000{image_url}"
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": image_url}
                    })

            formatted_messages.append({
                "role": msg["role"],
                "content": content_parts
            })

    try:
        json_data = {
            "model": "gpt-4o" if provider == "openai" else
            "claude-3-5-haiku" if provider == "claude" else
            "gemini-1.5-pro",
            "messages": formatted_messages,
            "max_tokens": 1024,
            "stream": True
        }

        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                    "POST",
                    f"{BASE_URL}/chat/completions",
                    headers=headers,
                    json=json_data
            ) as response:
                response.raise_for_status()

                async for chunk in response.aiter_text():
                    if chunk.strip():
                        if chunk.startswith("data: "):
                            yield chunk + "\n"
                        else:
                            yield f"data: {chunk}\n\n"
                        await asyncio.sleep(0.01)

    except httpx.HTTPStatusError as e:
        error_msg = f"API请求失败: {e.response.status_code}"
        if e.response.status_code == 400:
            error_detail = e.response.json().get("error", {}).get("message", "未知错误")
            error_msg += f" - {error_detail}"
        yield f"data: {json.dumps({'error': error_msg})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
@app.get("/")
async def index():
    return FileResponse("index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)