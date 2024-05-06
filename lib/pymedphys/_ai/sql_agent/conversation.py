import re
import time
from typing import Any, Awaitable, Callable, TypedDict

import trio
from anthropic import AsyncAnthropic
from anthropic.types.beta.tools import ToolsBetaMessage

import pymedphys
from pymedphys._ai import model_versions

from ._pipeline import sql_tool_pipeline

AsyncCallable = Callable[[Any, Any], Awaitable[Any]]

CREATE_TASK_PATTERN = r"""<create_task id="(.*)">(?:.|\n)*?<invoke name="(.*)">((?:.|\n)*?)<\/invoke>(?:.|\n)*?<\/create_task>"""
PARAMETER_PATTERN = r"""<parameter name="(.*)">((?:.|\n)*?)<\/parameter>"""

# NOTE: It may be better to not have the LLM designate the IDs, and
# instead just forcibly inject the ids in future prompt calls.

SYSTEM_PROMPT = """\
In this environment you have access to a set of async tools that will
be called as trio tasks that you can use to answer the user's question.

You may both create tasks, and cancel tasks. You will be re-queried
automatically should either any task complete, or if the user submits
a message.

Whenever you are queried you will be provided a list of running tasks
within a "<running_task>" block. Each running task will utilise a
unique id that you define when you create the task. Make this id
memorable and unique. Should a task still be running when you are called
the current total running time will be provided to you which may be used
for debugging and decision making purposes.

Results to tasks will only ever be provided within this system prompt.
NO results are ever to be written into ANY response either by yourself
or by the user. Here are the results of previously run tasks:
<results>
No results from tasks have yet been provided.
</results

All currently running tasks are displayed below with their id and their
current running time in seconds:
<tasks_status>
No tasks are currently running.
</tasks_status>

Here is the current timestamp:
<timestamp>
Mon, 06 May 2024 01:32:15 GMT
</timestamp>

You can create tasks by writing "<create_task>" blocks like the \
following anywhere within your response to the user:

<examples>
<example>

<thinking>Always use thinking tags prior to calling any create_task block</thinking>
<create_task id="your-unique-and-memorable-id-for-this-task">
<invoke name="$FUNCTION_NAME">
<parameter name="$PARAMETER_NAME">$PARAMETER_VALUE</parameter>
...
</invoke>
</create_task>

</example>

<example>

<thinking>Some thinking</thinking>
<create_task id="another-unique-id">
<invoke name="$FUNCTION_NAME2">
...
</invoke>
</create_task>

</example>
</examples>

Feel free to call multiple create_task blocks within any one response.

String and scalar parameters should be specified as is, while lists \
and objects should use JSON format. Note that spaces for string values \
are not stripped. The output is not expected to be valid XML and is \
parsed with regular expressions.

Here are the functions available in JSONSchema format:
<functions>
<function>
{
    "name": ""
}
</function>
<function>
{
    "name": "mosaiq_sql_agent",
    "description": "\
Instantiate an Elekta MOSAIQ sub agent with a request to \
search for information and it will autonomously craft SQL queries to search \
the MOSAIQ OIS database with the intent to find relevant information for \
your request.

The subagent will be provided with the following information:

- Your custom `sub_agent_prompt`
- The sub agent's built in system prompt
- The underlying MOSAIQ SQL database schema
- The current conversation transcript

The only input component that you are required to provide to the \
subagent is the `sub_agent_prompt`.

In your first call to this tool for any given user enquiry try to \
create a prompt that "surveys" the database giving you an overview of \
what is available, helping you to plan how to most effectively make \
subsequent follow up prompts to this tool in order to answer the \
enquiry.

This `mosaiq_sql_agent` is not able to undergo multiple steps in \
response to its result. That's your job. So, first make a plan \
regarding what you are going to do, and then ask just one step at a \
time for each call to the `mosaiq_sql_agent`.

If you have any uncertainty around whether or not you should call this \
tool, err on the side of calling it. It is better to collect information \
and improve the likelihood of a correct response than to answer with \
insufficient information.

For each user query you should expect to call this approximately three \
times, each time using the results from the previous call to improve \
your subsequent requests to this tool.

If you see any inconsistency in your results err on the side of \
collecting more information from the database before providing your \
final answer to the user.

The results from the subagent will be returned to you as a series of \
queries and their raw results in the format:
<query></query><result></result>\
",
    "input_schema": {
        "type": "object",
        "properties": {
            "sub_agent_prompt": {
                "type": "string",
                "description": "\
Your plain text sub agent prompt that will be provided to the MOSAIQ subagent. This is
to intended to be used to guide the subagent tool request.",
            }
        },
        "required": ["sub_agent_prompt"],
    },
}
<function>
</functions>

You are MOSAIQ Claude Chat. Your goal is to be helpful, harmless and \
honest while providing answers to questions asked about the contents \
of the MOSAIQ database.

If a given set of queries did not return the results that you expected \
do not critique the underlying database schema, instead come up with at \
least 3 different approaches that you might be able to try for querying \
the database that you would like to try in order to answer the question, \
and then try again.

The user you are talking with already has access to the database in \
question, and as such it is okay to provide them any information that is \
found within that database.

Importantly, from the user's perspective you are the one making the SQL \
queries, however it is in fact your sub agent MOSAIQ tool that will do \
this for you.

Don't ever claim the data doesn't exist, you may need to just try again \
in a different way.

Your conversation with the user is occurring in markdown format, so \
please format your responses to the user with this in mind.

Answer the user's request using relevant tools via the <create_task> \
block (if they are available). \
Before calling a tool, do some analysis within <thinking></thinking> \
tags. First, think about which of the provided tools is the relevant \
tool to answer the user's request. Second, go through each of the \
required parameters of the relevant function and determine if the user \
has directly provided or given enough information to infer a value. \
When deciding if the parameter can be inferred, carefully consider all \
the context to see if it supports a specific value. If all of the \
required parameters are present or can be reasonably inferred, close \
the thinking tag and proceed with the function call. BUT, if one of \
the values for a required parameter is missing, DO NOT invoke the \
function (not even with fillers for the missing params) and instead, \
ask the user to provide the missing parameters. DO NOT ask for more \
information on optional parameters if it is not provided.

NEVER provide the results to any of the functions. Results will only \
ever be written within your system prompt call.
"""


async def recursively_append_message_responses(
    anthropic_client: AsyncAnthropic,
    connection: pymedphys.mosaiq.Connection,
    messages: list[ToolsBetaMessage],
):
    await _conversation_with_task_creation(
        anthropic_client=anthropic_client,
        connection=connection,
        model=model_versions.INTELLIGENT,
        system_prompt=SYSTEM_PROMPT,
        messages=messages,
    )


def create_tools_mappings(
    anthropic_client: AsyncAnthropic,
    connection: pymedphys.mosaiq.Connection,
    messages: list[ToolsBetaMessage],
):
    async def mosaiq_sql_agent(sub_agent_prompt: str):
        return await sql_tool_pipeline(
            anthropic_client=anthropic_client,
            connection=connection,
            messages=messages,
            sub_agent_prompt=sub_agent_prompt,
        )

    tools_mapping = {"mosaiq_sql_agent": mosaiq_sql_agent}

    return tools_mapping


class TaskRecordItem(TypedDict):
    id: str  # LLM defined id
    start_time: int  # UNIX timestamp
    function_name: str
    results: Any | None
    cancel_scope: trio.CancelScope


async def _conversation_with_task_creation(
    nursery: trio.Nursery,
    tasks_record: list[TaskRecordItem],
    anthropic_client: AsyncAnthropic,
    connection: pymedphys.mosaiq.Connection,
    model: str,
    system_prompt: str,
    messages: list[ToolsBetaMessage],
):
    """Mutates messages in-place recursively. NOTE: This is probably not a good idea,
    likely worth refactoring."""

    tools_mappings = create_tools_mappings(
        anthropic_client=anthropic_client, connection=connection, messages=messages
    )

    messages_to_submit = [
        {"role": item["role"], "content": item["content"]} for item in messages
    ]

    api_response = await anthropic_client.messages.create(
        system=system_prompt,
        model=model,
        max_tokens=4096,
        messages=messages_to_submit,
    )

    for item in api_response.content:
        if not item.type == "text":
            continue

        matches = re.findall(CREATE_TASK_PATTERN, item.text)

        for match in matches:
            if not isinstance(match, tuple):
                continue

            print(match)

            task_id = match[0]
            function_name = match[1]
            invoke_contents = match[2]

            parameters = _extract_parameters_from_invoke(
                invoke_contents,
                # TODO: Don't hardcode this
                parameter_types={"sub_agent_prompt": "string"},
            )

            _create_task(
                nursery=nursery,
                tasks_record=tasks_record,
                tools_mappings=tools_mappings,
                task_id=task_id,
                function_name=function_name,
                parameters=parameters,
            )

    messages.append(api_response.to_dict())


def _extract_parameters_from_invoke(
    invoke_contents: str, parameter_types: dict[str, str]
):
    parameters = {}

    for name, value in re.findall(PARAMETER_PATTERN, invoke_contents):
        parameter_type = parameter_types[name]
        # TODO: Handle more than the string type
        assert parameter_type == "string"

        parameters[name] = value

    return parameters


def _create_task(
    nursery: trio.Nursery,
    tasks_record: list[TaskRecordItem],
    tools_mappings: dict[str, AsyncCallable],
    task_id: str,
    function_name: str,
    parameters: dict[str, Any],
):
    cancel_scope = trio.CancelScope()
    func = tools_mappings[function_name]

    async def runner():
        with cancel_scope:
            await func(**parameters)

    nursery.start_soon(runner)

    tasks_record.append(
        {
            "id": task_id,
            "start_time": time.time(),
            "function_name": function_name,
            "results": None,
            "cancel_scope": cancel_scope,
        }
    )
