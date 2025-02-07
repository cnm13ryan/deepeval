from typing import Optional, List, Union, Literal
from dataclasses import dataclass
from pydantic import create_model
import asyncio

from deepeval.metrics.dag.schema import (
    Reason,
    BinaryJudgementVerdict,
    NonBinaryJudgementVerdict,
)
from deepeval.metrics.dag.templates import (
    VerdictNodeTemplate,
    TaskNodeTemplate,
    BinaryJudgementTemplate,
    NonBinaryJudgementTemplate,
)
from deepeval.metrics.base_metric import BaseMetric
from deepeval.metrics.g_eval.g_eval import G_EVAL_PARAMS, GEval
from deepeval.metrics.utils import trimAndLoadJson
from deepeval.test_case import LLMTestCase, LLMTestCaseParams, ToolCall


class BaseNode:
    _indegree: Optional[int] = None

    def set_parent(self, parent: "BaseNode"):
        if hasattr(self, "_parent"):
            self._parent = parent
        elif hasattr(self, "_parents"):
            if self._parents is None:
                self._parents = []
            self._parents.append(parent)

    def _execute(self, metric: BaseMetric, test_case: LLMTestCase, depth: int):
        raise NotImplementedError(
            "This node type must implement the _execute method."
        )

    async def _a_execute(
        self, metric: BaseMetric, test_case: LLMTestCase, depth: int
    ):
        raise NotImplementedError(
            "This node type must implement the _a_execute method."
        )


def increment_indegree(node: BaseNode):
    if node._indegree is None:
        node._indegree = 1
    else:
        node._indegree += 1


def decrement_indegree(node: BaseNode):
    if node._indegree is None:
        node._indegree = 0
    else:
        node._indegree -= 1


@dataclass
class VerdictNode(BaseNode):
    verdict: Union[str, bool]
    score: Optional[int] = None
    g_eval: Optional[GEval] = None
    _parent: Optional[BaseNode] = None

    def __hash__(self):
        return id(self)

    def __post_init__(self):
        # Ensure either `score` or `g_eval` is set, but not both
        if self.score is not None and self.g_eval is not None:
            raise ValueError(
                "A VerdictNode can have either a 'score' or a 'g_eval', but not both."
            )
        if self.score is None and self.g_eval is None:
            raise ValueError(
                "A VerdictNode must have either a 'score' or a 'g_eval'."
            )

        if self.score is not None:
            if not (0 <= self.score <= 10):
                raise ValueError(
                    "The score must be between 0 and 10, inclusive."
                )

    def _execute(self, metric: BaseMetric, test_case: LLMTestCase, depth: int):
        decrement_indegree(self)
        if self._indegree != 0:
            return

        if isinstance(self._parent, NonBinaryJudgementNode) or isinstance(
            self._parent, BinaryJudgementNode
        ):
            if self._parent._verdict.verdict != self.verdict:
                return

        metric._verbose_steps.append(construct_node_verbose_log(self, depth))
        if self.g_eval is not None:
            g_eval_args = {
                "name": self.g_eval.name,
                "evaluation_params": self.g_eval.evaluation_params,
                "model": metric.model,
                "verbose_mode": metric.verbose_mode,
            }
            if self.g_eval.criteria:
                g_eval_args["criteria"] = self.g_eval.criteria
            else:
                g_eval_args["evaluation_steps"] = self.g_eval.evaluation_steps
            copied_g_eval = GEval(**g_eval_args)

            copied_g_eval.measure(test_case=test_case, _show_indicator=False)
            metric.score = copied_g_eval.score
            if metric.include_reason:
                metric.reason = copied_g_eval.reason
        else:
            metric.score = self.score / 10
            if metric.include_reason:
                metric.reason = self._generate_reason(metric=metric)

    async def _a_execute(
        self, metric: BaseMetric, test_case: LLMTestCase, depth: int
    ):
        decrement_indegree(self)
        if self._indegree != 0:
            return

        if isinstance(self._parent, NonBinaryJudgementNode) or isinstance(
            self._parent, BinaryJudgementNode
        ):
            if self._parent._verdict.verdict != self.verdict:
                return

        metric._verbose_steps.append(construct_node_verbose_log(self, depth))
        if self.g_eval is not None:
            g_eval_args = {
                "name": self.g_eval.name,
                "evaluation_params": self.g_eval.evaluation_params,
                "model": metric.model,
                "verbose_mode": metric.verbose_mode,
            }
            if self.g_eval.criteria:
                g_eval_args["criteria"] = self.g_eval.criteria
            else:
                g_eval_args["evaluation_steps"] = self.g_eval.evaluation_steps
            copied_g_eval = GEval(**g_eval_args)

            await copied_g_eval.a_measure(
                test_case=test_case, _show_indicator=False
            )
            metric.score = copied_g_eval.score
            if metric.include_reason:
                metric.reason = copied_g_eval.reason
        else:
            metric.score = self.score / 10
            if metric.include_reason:
                metric.reason = await self._a_generate_reason(metric=metric)

    def _generate_reason(self, metric: BaseMetric):
        prompt = VerdictNodeTemplate.generate_reason(
            verbose_steps=metric._verbose_steps,
            score=metric.score,
            name=metric.__name__,
        )
        if metric.using_native_model:
            res, cost = metric.model.generate(prompt, schema=Reason)
            metric.evaluation_cost += cost
        else:
            try:
                res: Reason = metric.model.generate(prompt, schema=Reason)
            except TypeError:
                res = metric.model.generate(prompt)
                data = trimAndLoadJson(res, self)
                res = Reason(**data)

        return res.reason

    async def _a_generate_reason(self, metric: BaseMetric):
        prompt = VerdictNodeTemplate.generate_reason(
            verbose_steps=metric._verbose_steps,
            score=metric.score,
            name=metric.__name__,
        )
        if metric.using_native_model:
            res, cost = await metric.model.a_generate(prompt, schema=Reason)
            metric.evaluation_cost += cost
        else:
            try:
                res: Reason = await metric.model.a_generate(
                    prompt, schema=Reason
                )
            except TypeError:
                res = await metric.model.a_generate(prompt)
                data = trimAndLoadJson(res, self)
                res = Reason(**data)

        return res.reason


@dataclass
class TaskNode(BaseNode):
    instructions: str
    output_label: str
    children: List[BaseNode]
    evaluation_params: List[LLMTestCaseParams] = None
    _verbose_logs: Optional[str] = None
    _output: Optional[str] = None
    _parents: Optional[List[BaseNode]] = None

    def __hash__(self):
        return id(self)

    def __post_init__(self):
        for child in self.children:
            if isinstance(child, VerdictNode):
                raise ValueError(
                    "A TaskNode must not have a VerdictNode as one of their 'children'."
                )

        for child in self.children:
            child.set_parent(self)
            increment_indegree(child)

    def _execute(self, metric: BaseMetric, test_case: LLMTestCase, depth: int):
        decrement_indegree(self)
        if self._indegree != 0:
            return

        text = """"""
        if self._parents is not None:
            for parent in self._parents:
                if isinstance(parent, TaskNode):
                    text += f"{parent.output_label}:\n{parent._output}\n\n"

        for param in self.evaluation_params:
            value = getattr(test_case, param.value)
            if isinstance(value, ToolCall):
                value = repr(value)
            text += f"{G_EVAL_PARAMS[param]}:\n{value}\n"

        prompt = TaskNodeTemplate.generate_task_output(
            instructions=self.instructions,
            text=text,
        )
        if metric.using_native_model:
            res, cost = metric.model.generate(prompt)
            metric.evaluation_cost += cost
            self._output = res
        else:
            res = metric.model.generate(prompt=prompt)
            self._output = res

        metric._verbose_steps.append(construct_node_verbose_log(self, depth))
        for children in self.children:
            children._execute(
                metric=metric, test_case=test_case, depth=depth + 1
            )

    async def _a_execute(
        self, metric: BaseMetric, test_case: LLMTestCase, depth: int
    ):
        decrement_indegree(self)
        if self._indegree != 0:
            return

        text = """"""
        if self._parents is not None:
            for parent in self._parents:
                if isinstance(parent, TaskNode):
                    text += f"{parent.output_label}:\n{parent._output}\n\n"

        for param in self.evaluation_params:
            value = getattr(test_case, param.value)
            if isinstance(value, ToolCall):
                value = repr(value)
            text += f"{G_EVAL_PARAMS[param]}:\n{value}\n"

        prompt = TaskNodeTemplate.generate_task_output(
            instructions=self.instructions,
            text=text,
        )

        if metric.using_native_model:
            res, cost = await metric.model.a_generate(prompt)
            metric.evaluation_cost += cost
            self._output = res
        else:
            res = await metric.model.a_generate(prompt=prompt)
            self._output = res

        metric._verbose_steps.append(construct_node_verbose_log(self, depth))
        await asyncio.gather(
            *(
                child._a_execute(
                    metric=metric, test_case=test_case, depth=depth + 1
                )
                for child in self.children
            )
        )


@dataclass
class BinaryJudgementNode(BaseNode):
    criteria: str
    children: List[VerdictNode]
    evaluation_params: Optional[List[LLMTestCaseParams]] = None
    _verbose_logs: Optional[str] = None
    _verdict: Optional[BinaryJudgementVerdict] = None
    _parents: Optional[List[BaseNode]] = None

    def __hash__(self):
        return id(self)

    def __post_init__(self):
        if len(self.children) != 2:
            raise ValueError(
                "BinaryJudgementNode must have exactly 2 children."
            )

        # Check if all children are ClassificationResultNode and their classifications are boolean
        for child in self.children:
            if not isinstance(child, VerdictNode):
                raise TypeError("All children must be of type VerdictNode.")
            if not isinstance(child.verdict, bool):
                raise ValueError(
                    "All children BinaryJudgementNode must have a boolean vedict."
                )

        # Check if there is one True and one False classification
        verdicts = [child.verdict for child in self.children]
        if verdicts.count(True) != 1 or verdicts.count(False) != 1:
            raise ValueError(
                "BinaryJudgementNode must have one True and one False VerdictNode child."
            )

        for child in self.children:
            child.set_parent(self)
            increment_indegree(child)

    def _execute(self, metric: BaseMetric, test_case: LLMTestCase, depth: int):
        decrement_indegree(self)
        if self._indegree != 0:
            return

        text = """"""
        for parent in self._parents:
            if isinstance(parent, TaskNode):
                text += f"{parent.output_label}:\n{parent._output}\n\n"

        if self.evaluation_params is not None:
            for param in self.evaluation_params:
                value = getattr(test_case, param.value)
                if isinstance(value, ToolCall):
                    value = repr(value)
                text += f"{G_EVAL_PARAMS[param]}:\n{value}\n"

        prompt = BinaryJudgementTemplate.generate_binary_verdict(
            criteria=self.criteria,
            text=text,
        )
        if metric.using_native_model:
            res, cost = metric.model.generate(
                prompt, schema=BinaryJudgementVerdict
            )
            metric.evaluation_cost += cost
            self._verdict = res
        else:
            try:
                res: BinaryJudgementVerdict = metric.model.generate(
                    prompt, schema=BinaryJudgementVerdict
                )
                self._verdict = res
            except TypeError:
                res = metric.model.generate(prompt)
                data = trimAndLoadJson(res, self)
                self._verdict = BinaryJudgementVerdict(**data)

        metric._verbose_steps.append(construct_node_verbose_log(self, depth))
        for children in self.children:
            children._execute(
                metric=metric, test_case=test_case, depth=depth + 1
            )

    async def _a_execute(
        self, metric: BaseMetric, test_case: LLMTestCase, depth: int
    ):
        decrement_indegree(self)
        if self._indegree != 0:
            return

        text = """"""
        for parent in self._parents:
            if isinstance(parent, TaskNode):
                text += f"{parent.output_label}:\n{parent._output}\n\n"

        if self.evaluation_params is not None:
            for param in self.evaluation_params:
                value = getattr(test_case, param.value)
                if isinstance(value, ToolCall):
                    value = repr(value)
                text += f"{G_EVAL_PARAMS[param]}:\n{value}\n"

        prompt = BinaryJudgementTemplate.generate_binary_verdict(
            criteria=self.criteria,
            text=text,
        )
        if metric.using_native_model:
            res, cost = await metric.model.a_generate(
                prompt, schema=BinaryJudgementVerdict
            )
            metric.evaluation_cost += cost
            self._verdict = res
        else:
            try:
                res: BinaryJudgementVerdict = await metric.model.a_generate(
                    prompt, schema=BinaryJudgementVerdict
                )
                self._verdict = res
            except TypeError:
                res = await metric.model.a_generate(prompt)
                data = trimAndLoadJson(res, self)
                self._verdict = BinaryJudgementVerdict(**data)

        metric._verbose_steps.append(construct_node_verbose_log(self, depth))
        await asyncio.gather(
            *(
                child._a_execute(
                    metric=metric, test_case=test_case, depth=depth + 1
                )
                for child in self.children
            )
        )


@dataclass
class NonBinaryJudgementNode(BaseNode):
    criteria: str
    children: List[VerdictNode]
    evaluation_params: Optional[List[LLMTestCaseParams]] = None
    _verbose_logs: Optional[str] = None
    _verdict: Optional[NonBinaryJudgementVerdict] = None
    _parents: Optional[List[BaseNode]] = None

    def __hash__(self):
        return id(self)

    def __post_init__(self):
        # Check if children is not empty
        if not self.children:
            raise ValueError(
                "NonBinaryJudgementNode must have at least one child."
            )

        verdicts_set = set()
        for child in self.children:
            if not isinstance(child, VerdictNode):
                raise TypeError("All children must be of type VerdictNode.")

            # Check if the verdict attribute of each child is a string
            if not isinstance(child.verdict, str):
                raise ValueError(
                    "The verdict attribute of all children must be a string."
                )

            # Check for duplicate verdicts
            if child.verdict in verdicts_set:
                raise ValueError(
                    f"Duplicate verdict found: {child.verdict} in children of NonBinaryJudgementNode."
                )
            verdicts_set.add(child.verdict)

        self._verdict_options = list(verdicts_set)

        # Dynamically create NonBinaryJudgementVerdict class
        self._verdict_schema = create_model(
            "NonBinaryJudgementVerdict",
            verdict=(Literal[tuple(self._verdict_options)], ...),
            reason=(str, ...),
        )

        for child in self.children:
            child.set_parent(self)
            increment_indegree(child)

    def _execute(self, metric: BaseMetric, test_case: LLMTestCase, depth: int):
        decrement_indegree(self)
        if self._indegree != 0:
            return

        text = """"""
        for parent in self._parents:
            if isinstance(parent, TaskNode):
                text += f"{parent.output_label}:\n{parent._output}\n"

        if self.evaluation_params is not None:
            for param in self.evaluation_params:
                value = getattr(test_case, param.value)
                if isinstance(value, ToolCall):
                    value = repr(value)
                text += f"{G_EVAL_PARAMS[param]}:\n{value}\n"

        prompt = NonBinaryJudgementTemplate.generate_non_binary_verdict(
            criteria=self.criteria, text=text, options=self._verdict_options
        )
        if metric.using_native_model:
            res, cost = metric.model.generate(
                prompt, schema=self._verdict_schema
            )
            metric.evaluation_cost += cost
            self._verdict = res
        else:
            try:
                res: self._verdict_schema = metric.model.generate(
                    prompt, schema=self._verdict_schema
                )
                self._verdict = res
            except TypeError:
                res = metric.model.generate(prompt)
                data = trimAndLoadJson(res, self)
                self._verdict = self._verdict_schema(**data)

        metric._verbose_steps.append(construct_node_verbose_log(self, depth))
        for children in self.children:
            children._execute(
                metric=metric, test_case=test_case, depth=depth + 1
            )

    async def _a_execute(
        self, metric: BaseMetric, test_case: LLMTestCase, depth: int
    ):
        decrement_indegree(self)
        if self._indegree != 0:
            return

        text = """"""
        for parent in self._parents:
            if isinstance(parent, TaskNode):
                text += f"{parent.output_label}:\n{parent._output}\n"

        if self.evaluation_params is not None:
            for param in self.evaluation_params:
                value = getattr(test_case, param.value)
                if isinstance(value, ToolCall):
                    value = repr(value)
                text += f"{G_EVAL_PARAMS[param]}:\n{value}\n"

        prompt = NonBinaryJudgementTemplate.generate_non_binary_verdict(
            criteria=self.criteria, text=text, options=self._verdict_options
        )
        if metric.using_native_model:
            res, cost = await metric.model.a_generate(
                prompt, schema=self._verdict_schema
            )
            metric.evaluation_cost += cost
            self._verdict = res
        else:
            try:
                res: self._verdict_schema = await metric.model.a_generate(
                    prompt, schema=self._verdict_schema
                )
                self._verdict = res
            except TypeError:
                res = await metric.model.a_generate(prompt)
                data = trimAndLoadJson(res, self)
                self._verdict = self._verdict_schema(**data)

        metric._verbose_steps.append(construct_node_verbose_log(self, depth))
        await asyncio.gather(
            *(
                child._a_execute(
                    metric=metric, test_case=test_case, depth=depth + 1
                )
                for child in self.children
            )
        )


def construct_node_verbose_log(node: BaseNode, depth: int) -> str:
    if isinstance(node, BinaryJudgementNode) or isinstance(
        node, NonBinaryJudgementNode
    ):
        is_binary_node = isinstance(node, BinaryJudgementNode)
        node_type = (
            "BinaryJudgementNode"
            if is_binary_node
            else "NonBinaryJudgementNode"
        )
        underscore_multiple = 34 if is_binary_node else 37
        star_multiple = 48 if is_binary_node else 56
        return (
            f"{'_' * underscore_multiple}\n"
            f"| {node_type} | Level == {depth} |\n"
            f"{'*' * star_multiple}\n"
            "Criteria:\n"
            f"{node.criteria}\n\n"
            f"Verdict: {node._verdict.verdict}\n"
            f"Reason: {node._verdict.reason}\n"
        )
    elif isinstance(node, TaskNode):
        return (
            "______________________\n"
            f"| TaskNode | Level == {depth} |\n"
            "*******************************\n"
            "Instructions:\n"
            f"{node.instructions}\n\n"
            f"{node.output_label}:\n{node._output}\n"
        )
    elif isinstance(node, VerdictNode):
        is_g_eval = node.g_eval is not None
        type = "GEval" if is_g_eval else "Deterministic"
        return (
            "________________________\n"
            f"| VerdictNode | Level == {depth} |\n"
            "**********************************\n"
            f"Verdict: {node.verdict}\n"
            f"Type: {type}"
        )
