
import datetime
import time
from typing import Optional, List, Any
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai import Task
from crewai.tasks.task_output import TaskOutput
from crewai.utilities.events.crewai_event_bus import crewai_event_bus
from crewai.utilities.events.task_events import (
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
)

def _execute_core(
        self,
        agent: Optional[BaseAgent],
        context: Optional[str],
        tools: Optional[List[Any]],
        max_retries: Optional[int] = 5,
        delay: Optional[int] = 5,
    ) -> TaskOutput:
        """Run the core execution logic of the task."""
        try:
            agent = agent or self.agent
            self.agent = agent
            if not agent:
                raise Exception(
                    f"The task '{self.description}' has no agent assigned, therefore it can't be executed directly and should be executed in a Crew using a specific process that support that, like hierarchical."
                )

            self.start_time = datetime.datetime.now()

            self.prompt_context = context
            tools = tools or self.tools or []

            self.processed_by_agents.add(agent.role)
            crewai_event_bus.emit(self, TaskStartedEvent(context=context, task=self))
            result = agent.execute_task(
                task=self,
                context=context,
                tools=tools,
            )

            pydantic_output, json_output = self._export_output(result)
            task_output = TaskOutput(
                name=self.name,
                description=self.description,
                expected_output=self.expected_output,
                raw=result,
                pydantic=pydantic_output,
                json_dict=json_output,
                agent=agent.role,
                output_format=self._get_output_format(),
            )

            if self._guardrail:
                guardrail_result = process_guardrail(
                    output=task_output,
                    guardrail=self._guardrail,
                    retry_count=self.retry_count,
                )
                if not guardrail_result.success:
                    if self.retry_count >= self.max_retries:
                        raise Exception(
                            f"Task failed guardrail validation after {self.max_retries} retries. "
                            f"Last error: {guardrail_result.error}"
                        )

                    self.retry_count += 1
                    context = self.i18n.errors("validation_error").format(
                        guardrail_result_error=guardrail_result.error,
                        task_output=task_output.raw,
                    )
                    printer = Printer()
                    printer.print(
                        content=f"Guardrail blocked, retrying, due to: {guardrail_result.error}\n",
                        color="yellow",
                    )
                    return self._execute_core(agent, context, tools, max_retries, delay)

                if guardrail_result.result is None:
                    raise Exception(
                        "Task guardrail returned None as result. This is not allowed."
                    )

                if isinstance(guardrail_result.result, str):
                    task_output.raw = guardrail_result.result
                    pydantic_output, json_output = self._export_output(
                        guardrail_result.result
                    )
                    task_output.pydantic = pydantic_output
                    task_output.json_dict = json_output
                elif isinstance(guardrail_result.result, TaskOutput):
                    task_output = guardrail_result.result

            self.output = task_output
            self.end_time = datetime.datetime.now()

            if self.callback:
                self.callback(self.output)

            crew = self.agent.crew  # type: ignore[union-attr]
            if crew and crew.task_callback and crew.task_callback != self.callback:
                crew.task_callback(self.output)

            if self.output_file:
                content = (
                    json_output
                    if json_output
                    else (
                        pydantic_output.model_dump_json() if pydantic_output else result
                    )
                )
                self._save_file(content)
            crewai_event_bus.emit(
                self, TaskCompletedEvent(output=task_output, task=self)
            )
            return task_output
        except Exception as e:
            self.end_time = datetime.datetime.now()
            crewai_event_bus.emit(self, TaskFailedEvent(error=str(e), task=self))
            if max_retries > 0:
                # Retry task execution
                time.sleep(delay)
                max_retries -= 1
                return self._execute_core(agent, context, tools, max_retries, delay)
            else:
                crewai_event_bus.emit(self, TaskCompletedEvent(output=TaskOutput(description="Task failed", agent=self.agent.role), task=self))
                #raise e  # Re-raise the exception after emitting the event

Task._execute_core = _execute_core