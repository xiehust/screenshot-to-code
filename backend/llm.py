from enum import Enum
from typing import Any, Awaitable, Callable, List, cast
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionChunk
from config import IS_DEBUG_ENABLED
from debug.DebugFileWriter import DebugFileWriter
# import boto3
from utils import pprint_prompt
import json
import os
import sys
import boto3
import base64
import requests
import time
import pprint
from datetime import datetime
from botocore.config import Config
	
#get modelARN
REGION = os.environ.get('region','us-east-1') 
PROFILE = os.environ.get('profile', 'default')
session = boto3.Session(profile_name=PROFILE,region_name=REGION)
bedrock_runtime = session.client(
    service_name="bedrock-runtime",
    region_name=REGION
)

# Actual model versions that are passed to the LLMs and stored in our logs
class Llm(Enum):
    GPT_4_VISION = "gpt-4-vision-preview"
    GPT_4_TURBO_2024_04_09 = "gpt-4-turbo-2024-04-09"
    GPT_4O_2024_05_13 = "gpt-4o-2024-05-13"
    CLAUDE_3_SONNET = "claude-3-sonnet-20240229"
    CLAUDE_3_5_SONNET = "claude-3-5-sonnet-20240620"
    CLAUDE_3_OPUS = "claude-3-opus-20240229"
    CLAUDE_3_HAIKU = "claude-3-haiku-20240307"
    CLAUDE_3_5_SONNET_2024_06_20 = "claude-3-5-sonnet-20240620"


BEDROCK_LLM_MODELID_LIST = {Llm.CLAUDE_3_5_SONNET: 'anthropic.claude-3-sonnet-20240229-v1:0',
                            Llm.CLAUDE_3_SONNET: 'anthropic.claude-3-5-sonnet-20240620-v1:0',}

# Will throw errors if you send a garbage string
def convert_frontend_str_to_llm(frontend_str: str) -> Llm:
    if frontend_str == "gpt_4_vision":
        return Llm.GPT_4_VISION
    elif frontend_str == "claude_3_sonnet":
        return Llm.CLAUDE_3_SONNET
    elif frontend_str == "claude_3_5_sonnet":
        return Llm.CLAUDE_3_5_SONNET
    else:
        return Llm(frontend_str)


async def stream_openai_response(
    messages: List[ChatCompletionMessageParam],
    api_key: str,
    base_url: str | None,
    callback: Callable[[str], Awaitable[None]],
    model: Llm,
) -> str:
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    print(f"--stream_openai_response--{model}")
    # Base parameters
    params = {
        "model": model.value,
        "messages": messages,
        "stream": True,
        "timeout": 600,
        "temperature": 0.0,
    }

    # Add 'max_tokens' only if the model is a GPT4 vision or Turbo model
    if (
        model == Llm.GPT_4_VISION
        or model == Llm.GPT_4_TURBO_2024_04_09
        or model == Llm.GPT_4O_2024_05_13
    ):
        params["max_tokens"] = 4096

    stream = await client.chat.completions.create(**params)  # type: ignore
    full_response = ""
    async for chunk in stream:  # type: ignore
        assert isinstance(chunk, ChatCompletionChunk)
        if (
            chunk.choices
            and len(chunk.choices) > 0
            and chunk.choices[0].delta
            and chunk.choices[0].delta.content
        ):
            content = chunk.choices[0].delta.content or ""
            full_response += content
            await callback(content)

    await client.close()

    return full_response


async def stream_claude_response(
    messages: List[ChatCompletionMessageParam],
    api_key: str,
    callback: Callable[[str], Awaitable[None]],
    model: Llm,
) -> str:
    print(f"--stream_openai_response--{model}")
    # client = AsyncAnthropic(api_key=api_key)
    modelId = BEDROCK_LLM_MODELID_LIST[model]
    # Base parameters
    max_tokens = 4096
    temperature = 0.0

    # Translate OpenAI messages to Claude messages
    system_prompt = cast(str, messages[0].get("content"))
    claude_messages = [dict(message) for message in messages[1:]]
    for message in claude_messages:
        if not isinstance(message["content"], list):
            continue

        for content in message["content"]:  # type: ignore
            if content["type"] == "image_url":
                content["type"] = "image"

                # Extract base64 data and media type from data URL
                # Example base64 data URL: data:image/png;base64,iVBOR...
                image_data_url = cast(str, content["image_url"]["url"])
                media_type = image_data_url.split(";")[0].split(":")[1]
                base64_data = image_data_url.split(",")[1]

                # Remove OpenAI parameter
                del content["image_url"]

                content["source"] = {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64_data
                }
    payload = {
        "modelId": modelId,
        "contentType": "application/json",
        "accept": "application/json",
        "body": {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": claude_messages,
            "temperature":temperature,
            "system":system_prompt,
        }
    }
    
    # Convert the payload to bytes
    body_bytes = json.dumps(payload['body']).encode('utf-8')
	
    # Invoke the model
    response = bedrock_runtime.invoke_model_with_response_stream(
        body=body_bytes, modelId=payload['modelId'], accept=payload['accept'], contentType=payload['contentType']
    )
    stream = response.get('body')
    chunk_obj = {}
    # Stream Claude response
    response_text = ''
    if stream:
        for event in stream:
            chunk = event.get('chunk')
            if chunk:
                chunk_obj = json.loads(chunk.get('bytes').decode())
                if chunk_obj['delta']['type'] == 'text_delta':
                    response_text += chunk_obj['delta']['text']
                    await callback(chunk_obj['delta']['text'])
                
    # Return final message
    response = response_text
    return response

async def stream_claude_response_native(
    system_prompt: str,
    messages: list[Any],
    api_key: str,
    callback: Callable[[str], Awaitable[None]],
    include_thinking: bool = False,
    model: Llm = Llm.CLAUDE_3_5_SONNET,
) -> str:

    # client = AsyncAnthropic(api_key=api_key)
    modelId = BEDROCK_LLM_MODELID_LIST[model]

    # Base model parameters
    max_tokens = 4096
    temperature = 0.0

    # Multi-pass flow
    current_pass_num = 1
    max_passes = 2

    prefix = "<thinking>"
    response = None

    # For debugging
    full_stream = ""
    debug_file_writer = DebugFileWriter()

    while current_pass_num <= max_passes:
        current_pass_num += 1

        # Set up message depending on whether we have a <thinking> prefix
        messages_to_send = (
            messages + [{"role": "assistant", "content": prefix}]
            if include_thinking
            else messages
        )

        # pprint_prompt(messages_to_send)
        
        payload = {
        "modelId": modelId,
        "contentType": "application/json",
        "accept": "application/json",
        "body": {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": messages_to_send,
            "temperature":temperature,
            "system":system_prompt,
            }
        }
        # Convert the payload to bytes
        body_bytes = json.dumps(payload['body']).encode('utf-8')
        
        # Invoke the model
        response = bedrock_runtime.invoke_model_with_response_stream(
            body=body_bytes, modelId=payload['modelId'], accept=payload['accept'], contentType=payload['contentType']
        )
        stream = response.get('body')
        chunk_obj = {}
        # Stream Claude response
        response_text = ''
        input_tokens= 0 
        output_tokens = 0
        if stream:
            for event in stream:
                chunk = event.get('chunk')
                if chunk:
                    chunk_obj = json.loads(chunk.get('bytes').decode())
                    # print(chunk_obj)
                    if chunk_obj['type'] == 'message_delta':
                        print(f"\nStop reason: {chunk_obj['delta']['stop_reason']}")
                        print(f"Stop sequence: {chunk_obj['delta']['stop_sequence']}")
                        print(f"Output tokens: {chunk_obj['usage']['output_tokens']}")
                        output_tokens = chunk_obj['usage']['output_tokens']

                    if chunk_obj['type'] == 'content_block_delta':
                        if chunk_obj['delta']['type'] == 'text_delta':
                            print(chunk_obj['delta']['text'], end="")
                            response_text += chunk_obj['delta']['text']
                            await callback(chunk_obj['delta']['text'])
                    
        print(response_text)

        # Write each pass's code to .html file and thinking to .txt file
        if IS_DEBUG_ENABLED:
            debug_file_writer.write_to_file(
                f"pass_{current_pass_num - 1}.html",
                debug_file_writer.extract_html_content(response_text),
            )
            debug_file_writer.write_to_file(
                f"thinking_pass_{current_pass_num - 1}.txt",
                response_text.split("</thinking>")[0],
            )

        # Set up messages array for next pass
        messages += [
            {"role": "assistant", "content": str(prefix) + response_text},
            {
                "role": "user",
                "content": "You've done a good job with a first draft. Improve this further based on the original instructions so that the app is fully functional and looks like the original video of the app we're trying to replicate.",
            },
        ]

        print(
            f"Token usage: Input Tokens: {input_tokens}, Output Tokens: {output_tokens}"
        )
        print(messages[-2:])

    # Close the Anthropic client
    # await client.close()

    if IS_DEBUG_ENABLED:
        debug_file_writer.write_to_file("full_stream.txt", full_stream)

    if not response:
        raise Exception("No HTML response found in AI response")
    else:
        return response_text
