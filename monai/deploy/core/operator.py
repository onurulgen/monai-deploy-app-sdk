# Copyright 2021 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional, Type, Union

from monai.deploy.exceptions import UnknownTypeError
from monai.deploy.utils.importutil import is_subclass

from .env import BaseEnv
from .execution_context import ExecutionContext
from .io_context import InputContext, OutputContext
from .io_type import IOType
from .operator_info import IO, OperatorInfo


class Operator(ABC):
    """This is the base Operator class.

    An operator in MONAI Deploy performs a unit of work for the application.
    An operator has multiple in/out and output ports.
    Each port specifies an interaction point through which a operator can
    communicate with other operators.
    """

    # Special attribute to identify the operator.
    # Used by is_subclass() from deploy.utils.importutil to
    # determine the operatorapplication to run.
    # This is needed to identify Operator class across different environments (e.g. by `runpy.run_path()`).
    _class_id: str = "monai.operator"

    _env: "OperatorEnv" = None

    def __init__(self, *args, **kwargs):
        """Constructor of the base operator.

        It creates an instance of Data Store which holds on
        to all inputs and outputs relavant for this operator.
        """
        super().__init__()
        self._uid = uuid.uuid4()
        self._op_info = OperatorInfo()

        # Execute the builder to set up the operator
        self._builder()

    @classmethod
    def __subclasshook__(cls, c: Type) -> bool:
        return is_subclass(c, cls._class_id)

    def _builder(self):
        """This method is called by the constructor of Operator to set up the operator.

        This method returns `self` to allow for method chaining and new `_builder()` method is
        chained by decorators.

        Returns:
            An instance of Operator.
        """
        return self

    def __hash__(self):
        return hash(self._uid)

    def __eq__(self, other):
        return self._uid == other._uid

    def add_input(self, label: str, data_type: Type, storage_type: Union[int, IOType]):
        self._op_info.add_label(IO.INPUT, label)
        self._op_info.set_data_type(IO.INPUT, label, data_type)
        self._op_info.set_storage_type(IO.INPUT, label, storage_type)

    def add_output(self, label: str, data_type: Type, storage_type: Union[int, IOType]):
        self._op_info.add_label(IO.OUTPUT, label)
        self._op_info.set_data_type(IO.OUTPUT, label, data_type)
        self._op_info.set_storage_type(IO.OUTPUT, label, storage_type)

    @property
    def name(self):
        """Returns the name of this operator."""
        return self.__class__.__name__

    @property
    def uid(self):
        """Gives access to the UID of the operator.

        Returns:
            UID of the operator.
        """
        return self._uid

    @property
    def op_info(self):
        """Retrieves the operator info.

        Args:

        Returns:
            An instance of OperatorInfo.

        """
        return self._op_info

    @property
    def env(self):
        """Gives access to the environment.

        This sets a default value for the operator's environment if not set.

        Returns:
            An instance of OperatorEnv.
        """
        if self._env is None:
            self._env = OperatorEnv()
        return self._env

    def ensure_valid(self):
        """Ensures that the operator is valid.

        This method needs to be executed by `add_operator()` and `add_flow()` methods in the `compose()` method of the
        application.
        This sets default values for the operator in the graph if necessary.
        (e.g., set default value for the operator's input port, set default
        value for the operator's output port, etc.)
        """
        self.op_info.ensure_valid()

    def pre_compute(self):
        """This method gets executed before `compute()` of an operator is called.

        This is a preperatory step before the operator executes its main job.
        This needs to be overridden by a base class for any meaningful action.
        """
        pass

    @abstractmethod
    def compute(self, input: InputContext, output: OutputContext, context: ExecutionContext):
        """An abstract method that needs to be implemented by the user.

        Args:
            input (InputContext): An input context for the operator.
            output (OutputContext): An output context for the operator.
            context (ExecutionContext): An execution context for the operator.
        """
        pass

    def post_compute(self):
        """This method gets executed after "compute()" of an operator is called.

        This is a post-execution step before the operator is done doing its
        main action.
        This needs to be overridden by a base class for any meaningful action.
        """
        pass


def input(label: str = "", data_type: Type = Any, storage_type: Union[int, IOType] = IOType.UNKNOWN):
    """A decorator that adds input specification to the operator.

    Args:
        label (str): A label for the input port.
        data_type (Type): A data type of the input.
        storage_type (Union[int, IOType]): A storage type of the input.

    Returns:
        A decorator that adds input specification to the operator.
    """

    def decorator(cls):
        if issubclass(cls, Operator):
            builder = cls.__dict__.get("_builder")
        else:
            raise UnknownTypeError("Use @input decorator only for a subclass of Operator!")

        def new_builder(self: Operator):
            # Execute (this) outer decorator first so decorators are executed in order
            self.add_input(label, data_type, storage_type)
            if builder:
                builder(self)  # execute the original builder
            return self

        cls._builder = new_builder
        return cls

    return decorator


def output(label: str = "", data_type: Type = Any, storage_type: Union[int, IOType] = IOType.UNKNOWN):
    """A decorator that adds output specification to the operator.

    Args:
        label (str): A label for the output port.
        data_type (Type): A data type of the output.
        storage_type (Union[int, IOType]): A storage type of the output.

    Returns:
        A decorator that adds output specification to the operator.
    """

    def decorator(cls):
        if issubclass(cls, Operator):
            builder = cls.__dict__.get("_builder")
        else:
            raise UnknownTypeError("Use @output decorator only for a subclass of Operator!")

        def new_builder(self: Operator):
            # Execute (this) outer decorator first so decorators are executed in order
            self.add_output(label, data_type, storage_type)
            if builder:
                builder(self)  # execute the original builder
            return self

        cls._builder = new_builder
        return cls

    return decorator


class OperatorEnv(BaseEnv):
    """Settings for the operator environment.

    This class is used to specify the environment settings for the operator.
    """

    pass
