import asyncio
import re
import time

import httpx
from typing import List, Optional
from abc import ABC
from openai import OpenAI
from ollama import Client
from starlette.concurrency import run_in_threadpool
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
import google.generativeai as genai

from config.config import Configs
from db.repository.conversation_repository import add_conversation_to_db
from db.repository.message_repository import (
    get_conversation_messages,
    add_message_to_db,
)
from rag.kb.api.kb_doc_api import search_docs
from rag.reranker.reranker import LangchainReranker
from server.utils.utils import LLMType, replace_ip_with_targetip
from utils.log_common import build_logger

logger = build_logger()


class OpenAIChat(ABC):
    def __init__(self, config):
        self.config = config
        self.client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=config.timeout,
        )
        self.model_name = self.config.llm_model_name

    @retry(
        stop=stop_after_attempt(3),  # Stop after 3 attempts
    )
    def chat(self, history: List) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=history,
                temperature=self.config.temperature,
            )
            ans = response.choices[0].message.content
            return ans
        except (
            httpx.HTTPStatusError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            ConnectionError,
        ) as e:
            if getattr(e, "response", None) and e.response.status_code == 429:
                # Rate limit error, wait longer
                time.sleep(2)
            raise  # Re-raise the exception to trigger retry
        except Exception as e:
            return f"**ERROR**: {str(e)}"


class OllamaChat(ABC):
    def __init__(self, config):
        self.config = config
        self.client = Client(host=self.config.base_url)
        self.model_name = self.config.llm_model_name

    def chat(self, history: List[dict]) -> str:

        try:
            options = {
                "temperature": self.config.temperature,
            }
            response = self.client.chat(
                model=self.model_name, messages=history, options=options, keep_alive=-1
            )
            ans = response["message"]["content"]
            return ans
        except httpx.HTTPStatusError as e:
            return f"**ERROR**: {str(e)}"


class GeminiChat(ABC):
    def __init__(self, config):
        self.config = config
        # Configure the Gemini API key
        genai.configure(api_key=self.config.api_key)
        self.model = genai.GenerativeModel(self.config.llm_model_name)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(
            (
                httpx.HTTPStatusError,
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                ConnectionError,
            )
        ),
    )
    def chat(self, history: List) -> str:
        # Gemini có một định dạng history hơi khác, cần chuyển đổi
        # Nó không chấp nhận vai trò "system" ở đầu, và các vai trò phải xen kẽ user/model
        gemini_history = []
        # Bỏ qua system prompt nếu có
        for msg in history:
            if msg["role"] == "system":
                continue
            # Đổi 'assistant' thành 'model' cho Gemini
            role = "model" if msg["role"] == "assistant" else "user"
            gemini_history.append({"role": role, "parts": [msg["content"]]})

        try:
            # Loại bỏ message cuối cùng của 'model' nếu có, vì Gemini không cho phép 2 message cùng role liên tiếp
            if gemini_history and len(gemini_history) > 1:
                if gemini_history[-1]["role"] == gemini_history[-2]["role"]:
                    gemini_history.pop(-2)

            response = self.model.generate_content(
                gemini_history,
                generation_config=genai.types.GenerationConfig(
                    temperature=self.config.temperature
                ),
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            # Xử lý các lỗi cụ thể từ API của Google nếu cần
            # Ví dụ: response.prompt_feedback
            if hasattr(e, "response") and hasattr(e.response, "prompt_feedback"):
                return f"**ERROR**: Blocked by Gemini API. Reason: {e.response.prompt_feedback}"
            return f"**ERROR**: {str(e)}"


def _chat(query: str, kb_name=None, conversation_id=None, kb_query=None, summary=True):
    try:
        if Configs.basic_config.enable_rag and kb_name is not None:
            docs = asyncio.run(
                run_in_threadpool(
                    search_docs,
                    query=kb_query,
                    knowledge_base_name=kb_name,
                    top_k=Configs.kb_config.top_k,
                    score_threshold=Configs.kb_config.score_threshold,
                    file_name="",
                    metadata={},
                )
            )

            reranker_model = LangchainReranker(
                top_n=Configs.kb_config.top_n,
                name_or_path=Configs.llm_config.rerank_model,
            )

            docs = reranker_model.compress_documents(documents=docs, query=kb_query)

            if len(docs) == 0:
                context = ""
            else:
                context = "\n".join([doc["page_content"] for doc in docs])

            if context:
                context = replace_ip_with_targetip(context)
                query = f"{query}\n\n\n Ensure that the **Overall Target** IP or the IP from the **Initial Description** is prioritized. You will respond to questions and generate tasks based on the provided penetration test case materials: {context}. \n"

        if conversation_id is not None and len(query) > 10000:
            query = query[:10000]
        else:
            query = query[: Configs.llm_config.context_length]

        flag = False

        if conversation_id is not None:
            flag = True

        # Initialize or retrieve conversation ID
        conversation_id = add_conversation_to_db(
            Configs.llm_config.llm_model_name, conversation_id
        )

        history = [
            {
                "role": "system",
                "content": "You are a helpful assistant",
            }
        ]
        # Retrieve message history from database, and limit the number of messages
        for msg in get_conversation_messages(conversation_id)[
            -Configs.llm_config.history_len :
        ]:
            history.append({"role": "user", "content": msg.query})
            history.append({"role": "assistant", "content": msg.response})

        # Add user query to the message history
        history.append({"role": "user", "content": query})

        # Initialize the correct model client
        if Configs.llm_config.llm_model == LLMType.OPENAI:
            client = OpenAIChat(config=Configs.llm_config)
        elif Configs.llm_config.llm_model == LLMType.OLLAMA:
            client = OllamaChat(config=Configs.llm_config)
        elif Configs.llm_config.llm_model == LLMType.GEMINI:
            client = GeminiChat(config=Configs.llm_config)
        else:
            return "Unsupported model type"

        # Get response from the model
        response_text = client.chat(history)

        # Save both query and response to the database
        if summary:
            add_message_to_db(
                conversation_id, Configs.llm_config.llm_model_name, query, response_text
            )

        if flag:
            return response_text
        else:
            return response_text, conversation_id

    except Exception as e:
        print(e)
        return f"**ERROR**: {str(e)}"
