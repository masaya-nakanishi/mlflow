import inspect
import itertools
import functools
import warnings
import logging
import time
import contextlib
from abc import abstractmethod

import mlflow
from mlflow.entities.run_status import RunStatus
from mlflow.utils import gorilla
from mlflow.entities import Metric
from mlflow.tracking.client import MlflowClient
from mlflow.utils.validation import MAX_METRICS_PER_BATCH


INPUT_EXAMPLE_SAMPLE_ROWS = 5
ENSURE_AUTOLOGGING_ENABLED_TEXT = (
    "please ensure that autologging is enabled before constructing the dataset."
)
_AUTOLOGGING_TEST_MODE_ENV_VAR = "MLFLOW_AUTOLOGGING_TESTING"

# Dict mapping integration name to its config.
AUTOLOGGING_INTEGRATIONS = {}

_logger = logging.getLogger(__name__)


def try_mlflow_log(fn, *args, **kwargs):
    """
    Catch exceptions and log a warning to avoid autolog throwing.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # pylint: disable=broad-except
        if _is_testing():
            raise
        else:
            warnings.warn("Logging to MLflow failed: " + str(e), stacklevel=2)


def log_fn_args_as_params(fn, args, kwargs, unlogged=[]):  # pylint: disable=W0102
    """
    Log parameters explicitly passed to a function.

    :param fn: function whose parameters are to be logged
    :param args: arguments explicitly passed into fn. If `fn` is defined on a class,
                 `self` should not be part of `args`; the caller is responsible for
                 filtering out `self` before calling this function.
    :param kwargs: kwargs explicitly passed into fn
    :param unlogged: parameters not to be logged
    :return: None
    """
    param_spec = inspect.signature(fn).parameters
    # Filter out `self` from the signature under the assumption that it is not contained
    # within the specified `args`, as stipulated by the documentation
    relevant_params = [param for param in param_spec.values() if param.name != "self"]

    # Fetch the parameter names for specified positional arguments from the function
    # signature & create a mapping from positional argument name to specified value
    params_to_log = {
        param_info.name: param_val
        for param_info, param_val in zip(list(relevant_params)[: len(args)], args)
    }
    # Add all user-specified keyword arguments to the set of parameters to log
    params_to_log.update(kwargs)
    # Add parameters that were not explicitly specified by the caller to the mapping,
    # using their default values
    params_to_log.update(
        {
            param.name: param.default
            for param in list(relevant_params)[len(args) :]
            if param.name not in kwargs
        }
    )
    # Filter out any parameters that should not be logged, as specified by the `unlogged` parameter
    params_to_log = {key: value for key, value in params_to_log.items() if key not in unlogged}
    try_mlflow_log(mlflow.log_params, params_to_log)


def _update_wrapper_extended(wrapper, wrapped):
    """
    Update a `wrapper` function to look like the `wrapped` function. This is an extension of
    `functools.update_wrapper` that applies the docstring *and* signature of `wrapped` to
    `wrapper`, producing a new function.

    :return: A new function with the same implementation as `wrapper` and the same docstring
             & signature as `wrapped`.
    """
    updated_wrapper = functools.update_wrapper(wrapper, wrapped)
    # Assign the signature of the `wrapped` function to the updated wrapper function.
    # Certain frameworks may disallow signature inspection, causing `inspect.signature()` to throw.
    # One such example is the `tensorflow.estimator.Estimator.export_savedmodel()` function
    try:
        updated_wrapper.__signature__ = inspect.signature(wrapped)
    except Exception:  # pylint: disable=broad-except
        _logger.debug("Failed to restore original signature for wrapper around %s", wrapped)
    return updated_wrapper


def wrap_patch(destination, name, patch, settings=None):
    """
    Apply a patch while preserving the attributes (e.g. __doc__) of an original function.

    TODO(dbczumar): Convert this to an internal method once existing `wrap_patch` calls
                    outside of `autologging_utils` have been converted to `safe_patch`

    :param destination: Patch destination
    :param name: Name of the attribute at the destination
    :param patch: Patch function
    :param settings: Settings for gorilla.Patch
    """
    if settings is None:
        settings = gorilla.Settings(allow_hit=True, store_hit=True)

    original = getattr(destination, name)
    wrapped = _update_wrapper_extended(patch, original)

    patch = gorilla.Patch(destination, name, wrapped, settings=settings)
    gorilla.apply(patch)


class _InputExampleInfo:
    """
    Stores info about the input example collection before it is needed.

    For example, in xgboost and lightgbm, an InputExampleInfo object is attached to the dataset,
    where its value is read later by the train method.

    Exactly one of input_example or error_msg should be populated.
    """

    def __init__(self, input_example=None, error_msg=None):
        self.input_example = input_example
        self.error_msg = error_msg


def resolve_input_example_and_signature(
    get_input_example, infer_model_signature, log_input_example, log_model_signature, logger
):
    """
    Handles the logic of calling functions to gather the input example and infer the model
    signature.

    :param get_input_example: function which returns an input example, usually sliced from a
                              dataset. This function can raise an exception, its message will be
                              shown to the user in a warning in the logs.
    :param infer_model_signature: function which takes an input example and returns the signature
                                  of the inputs and outputs of the model. This function can raise
                                  an exception, its message will be shown to the user in a warning
                                  in the logs.
    :param log_input_example: whether to log errors while collecting the input example, and if it
                              succeeds, whether to return the input example to the user. We collect
                              it even if this parameter is False because it is needed for inferring
                              the model signature.
    :param log_model_signature: whether to infer and return the model signature.
    :param logger: the logger instance used to log warnings to the user during input example
                   collection and model signature inference.

    :return: A tuple of input_example and signature. Either or both could be None based on the
             values of log_input_example and log_model_signature.
    """

    input_example = None
    input_example_user_msg = None
    input_example_failure_msg = None
    if log_input_example or log_model_signature:
        try:
            input_example = get_input_example()
        except Exception as e:  # pylint: disable=broad-except
            input_example_failure_msg = str(e)
            input_example_user_msg = "Failed to gather input example: " + str(e)

    model_signature = None
    model_signature_user_msg = None
    if log_model_signature:
        try:
            if input_example is None:
                raise Exception(
                    "could not sample data to infer model signature: " + input_example_failure_msg
                )
            model_signature = infer_model_signature(input_example)
        except Exception as e:  # pylint: disable=broad-except
            model_signature_user_msg = "Failed to infer model signature: " + str(e)

    if log_input_example and input_example_user_msg is not None:
        logger.warning(input_example_user_msg)
    if log_model_signature and model_signature_user_msg is not None:
        logger.warning(model_signature_user_msg)

    return input_example if log_input_example else None, model_signature


class BatchMetricsLogger:
    """
    The BatchMetricsLogger will log metrics in batch against an mlflow run.
    If run_id is passed to to constructor then all recording and logging will
    happen against that run_id.
    If no run_id is passed into constructor, then the run ID will be fetched
    from `mlflow.active_run()` each time `record_metrics()` or `flush()` is called; in this
    case, callers must ensure that an active run is present before invoking
    `record_metrics()` or `flush()`.
    """

    def __init__(self, run_id=None):
        self.run_id = run_id

        # data is an array of Metric objects
        self.data = []
        self.total_training_time = 0
        self.total_log_batch_time = 0
        self.previous_training_timestamp = None

    def flush(self):
        """
        The metrics accumulated by BatchMetricsLogger will be batch logged to an MLFlow run.
        """
        self._timed_log_batch()
        self.data = []

    def _timed_log_batch(self):
        if self.run_id is None:
            # Retrieving run_id from active mlflow run.
            current_run_id = mlflow.active_run().info.run_id
        else:
            current_run_id = self.run_id

        start = time.time()
        metrics_slices = [
            self.data[i : i + MAX_METRICS_PER_BATCH]
            for i in range(0, len(self.data), MAX_METRICS_PER_BATCH)
        ]
        for metrics_slice in metrics_slices:
            try_mlflow_log(MlflowClient().log_batch, run_id=current_run_id, metrics=metrics_slice)
        end = time.time()
        self.total_log_batch_time += end - start

    def _should_flush(self):
        target_training_to_logging_time_ratio = 10
        if (
            self.total_training_time
            >= self.total_log_batch_time * target_training_to_logging_time_ratio
        ):
            return True

        return False

    def record_metrics(self, metrics, step=None):
        """
        Submit a set of metrics to be logged. The metrics may not be immediately logged, as this
        class will batch them in order to not increase execution time too much by logging
        frequently.

        :param metrics: dictionary containing key, value pairs of metrics to be logged.
        :param step: the training step that the metrics correspond to.
        """
        current_timestamp = time.time()
        if self.previous_training_timestamp is None:
            self.previous_training_timestamp = current_timestamp

        training_time = current_timestamp - self.previous_training_timestamp

        self.total_training_time += training_time

        # log_batch() requires step to be defined. Therefore will set step to 0 if not defined.
        if step is None:
            step = 0

        for key, value in metrics.items():

            self.data.append(Metric(key, value, int(current_timestamp * 1000), step))

        if self._should_flush():
            self.flush()

        self.previous_training_timestamp = current_timestamp


@contextlib.contextmanager
def batch_metrics_logger(run_id):
    """
    Context manager that yields a BatchMetricsLogger object, which metrics can be logged against.
    The BatchMetricsLogger keeps metrics in a list until it decides they should be logged, at
    which point the accumulated metrics will be batch logged. The BatchMetricsLogger ensures
    that logging imposes no more than a 10% overhead on the training, where the training is
    measured by adding up the time elapsed between consecutive calls to record_metrics.

    If logging a batch fails, a warning will be emitted and subsequent metrics will continue to
    be collected.

    Once the context is closed, any metrics that have yet to be logged will be logged.

    :param run_id: ID of the run that the metrics will be logged to.
    """

    batch_metrics_logger = BatchMetricsLogger(run_id)
    yield batch_metrics_logger
    batch_metrics_logger.flush()


def autologging_integration(name):
    """
    **All autologging integrations should be decorated with this wrapper.**

    Wraps an autologging function in order to store its configuration arguments. This enables
    patch functions to broadly obey certain configurations (e.g., disable=True) without
    requiring specific logic to be present in each autologging integration.
    """

    def validate_param_spec(param_spec):
        if "disable" not in param_spec or param_spec["disable"].default is not False:
            raise Exception(
                "Invalid `autolog()` function for integration '{}'. `autolog()` functions"
                " must specify a 'disable' argument with default value 'False'".format(name)
            )

    def wrapper(_autolog):
        param_spec = inspect.signature(_autolog).parameters
        validate_param_spec(param_spec)

        AUTOLOGGING_INTEGRATIONS[name] = {}
        default_params = {param.name: param.default for param in param_spec.values()}

        def autolog(*args, **kwargs):
            config_to_store = dict(default_params)
            config_to_store.update(
                {param.name: arg for arg, param in zip(args, param_spec.values())}
            )
            config_to_store.update(kwargs)
            AUTOLOGGING_INTEGRATIONS[name] = config_to_store

            return _autolog(*args, **kwargs)

        wrapped_autolog = _update_wrapper_extended(autolog, _autolog)
        return wrapped_autolog

    return wrapper


def get_autologging_config(flavor_name, config_key, default_value=None):
    """
    Returns a desired config value for a specified autologging integration.
    Returns `None` if specified `flavor_name` has no recorded configs.
    If `config_key` is not set on the config object, default value is returned.

    :param flavor_name: An autologging integration flavor name.
    :param config_key: The key for the desired config value.
    :param default_value: The default_value to return
    """
    config = AUTOLOGGING_INTEGRATIONS.get(flavor_name)
    if config is not None:
        return config.get(config_key, default_value)
    else:
        return default_value


def autologging_is_disabled(flavor_name):
    """
    Returns a boolean flag of whether the autologging integration is disabled.

    :param flavor_name: An autologging integration flavor name.
    """
    return get_autologging_config(flavor_name, "disable", True)


def _is_testing():
    """
    Indicates whether or not autologging functionality is running in test mode (as determined
    by the `MLFLOW_AUTOLOGGING_TESTING` environment variable). Test mode performs additional
    validation during autologging, including:

        - Checks for the exception safety of arguments passed to model training functions
          (i.e. all additional arguments should be "exception safe" functions or classes)
        - Disables exception handling for patched function logic, ensuring that patch code
          executes without errors during testing
    """
    import os

    return os.environ.get(_AUTOLOGGING_TEST_MODE_ENV_VAR, "false") == "true"


# Function attribute used for testing purposes to verify that a given function
# has been wrapped with the `exception_safe_function` decorator
_ATTRIBUTE_EXCEPTION_SAFE = "exception_safe"


def exception_safe_function(function):
    """
    Wraps the specified function with broad exception handling to guard
    against unexpected errors during autologging.
    """
    if _is_testing():
        setattr(function, _ATTRIBUTE_EXCEPTION_SAFE, True)

    def safe_function(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as e:  # pylint: disable=broad-except
            if _is_testing():
                raise
            else:
                _logger.warning("Encountered unexpected error during autologging: %s", e)

    safe_function = _update_wrapper_extended(safe_function, function)
    return safe_function


class ExceptionSafeClass(type):
    """
    Metaclass that wraps all functions defined on the specified class with broad error handling
    logic to guard against unexpected errors during autlogging.

    Rationale: Patched autologging functions commonly pass additional class instances as arguments
    to their underlying original training routines; for example, Keras autologging constructs
    a subclass of `keras.callbacks.Callback` and forwards it to `Model.fit()`. To prevent errors
    encountered during method execution within such classes from disrupting model training,
    this metaclass wraps all class functions in a broad try / catch statement.

    Note: `ExceptionSafeClass` does not handle exceptions in class methods or static methods,
    as these are not always Python callables and are difficult to wrap
    """

    def __new__(cls, name, bases, dct):
        for m in dct:
            if callable(dct[m]):
                dct[m] = exception_safe_function(dct[m])
        return type.__new__(cls, name, bases, dct)


class PatchFunction:
    """
    Base class representing a function patch implementation with a callback for error handling.
    `PatchFunction` should be subclassed and used in conjunction with `safe_patch` to
    safely modify the implementation of a function. Subclasses of `PatchFunction` should
    use `_patch_implementation` to define modified ("patched") function implementations and
    `_on_exception` to define cleanup logic when `_patch_implementation` terminates due
    to an unhandled exception.
    """

    @abstractmethod
    def _patch_implementation(self, original, *args, **kwargs):
        """
        Invokes the patch function code.

        :param original: The original, underlying function over which the `PatchFunction`
                         is being applied.
        :param *args: The positional arguments passed to the original function.
        :param **kwargs: The keyword arguments passed to the original function.
        """
        pass

    @abstractmethod
    def _on_exception(self, exception):
        """
        Called when an unhandled exception prematurely terminates the execution
        of `_patch_implementation`.

        :param exception: The unhandled exception thrown by `_patch_implementation`.
        """
        pass

    @classmethod
    def call(cls, original, *args, **kwargs):
        return cls().__call__(original, *args, **kwargs)

    def __call__(self, original, *args, **kwargs):
        try:
            return self._patch_implementation(original, *args, **kwargs)
        except Exception as e:  # pylint: disable=broad-except
            try:
                self._on_exception(e)
            finally:
                # Regardless of what happens during the `_on_exception` callback, reraise
                # the original implementation exception once the callback completes
                raise e


def with_managed_run(patch_function):
    """
    Given a `patch_function`, returns an `augmented_patch_function` that wraps the execution of
    `patch_function` with an active MLflow run. The following properties apply:

        - An MLflow run is only created if there is no active run present when the
          patch function is executed

        - If an active run is created by the `augmented_patch_function`, it is terminated
          with the `FINISHED` state at the end of function execution

        - If an active run is created by the `augmented_patch_function`, it is terminated
          with the `FAILED` if an unhandled exception is thrown during function execution

    Note that, if nested runs or non-fluent runs are created by `patch_function`, `patch_function`
    is responsible for terminating them by the time it terminates (or in the event of an exception).

    :param patch_function: A `PatchFunction` class definition or a function object
                           compatible with `safe_patch`.
    """

    if inspect.isclass(patch_function):

        class PatchWithManagedRun(patch_function):
            def __init__(self):
                super(PatchWithManagedRun, self).__init__()
                self.managed_run = None

            def _patch_implementation(self, original, *args, **kwargs):
                if not mlflow.active_run():
                    self.managed_run = try_mlflow_log(mlflow.start_run)

                result = super(PatchWithManagedRun, self)._patch_implementation(
                    original, *args, **kwargs
                )

                if self.managed_run:
                    try_mlflow_log(mlflow.end_run, RunStatus.to_string(RunStatus.FINISHED))

                return result

            def _on_exception(self, e):
                if self.managed_run:
                    try_mlflow_log(mlflow.end_run, RunStatus.to_string(RunStatus.FAILED))
                super(PatchWithManagedRun, self)._on_exception(e)

        return PatchWithManagedRun

    else:

        def patch_with_managed_run(original, *args, **kwargs):
            managed_run = None
            if not mlflow.active_run():
                managed_run = try_mlflow_log(mlflow.start_run)

            try:
                result = patch_function(original, *args, **kwargs)
            except:
                if managed_run:
                    try_mlflow_log(mlflow.end_run, RunStatus.to_string(RunStatus.FAILED))
                raise
            else:
                if managed_run:
                    try_mlflow_log(mlflow.end_run, RunStatus.to_string(RunStatus.FINISHED))
                return result

        return patch_with_managed_run


def safe_patch(
    autologging_integration, destination, function_name, patch_function, manage_run=False
):
    """
    Patches the specified `function_name` on the specified `destination` class for autologging
    purposes, replacing its implementation with an error-safe copy of the specified patch
    `function` with the following error handling behavior:

        - Exceptions thrown from the underlying / original function
          (`<destination>.<function_name>`) are propagated to the caller.

        - Exceptions thrown from other parts of the patched implementation (`patch_function`)
          are caught and logged as warnings.


    :param autologging_integration: The name of the autologging integration associated with the
                                    patch.
    :param destination: The Python class on which the patch is being defined.
    :param function_name: The name of the function to patch on the specified `destination` class.
    :param patch_function: The patched function code to apply. This is either a `PatchFunction`
                           class definition or a function object. If it is a function object, the
                           first argument should be reserved for an `original` method argument
                           representing the underlying / original function. Subsequent arguments
                           should be identical to those of the original function being patched.
    :param manage_run: If `True`, applies the `with_managed_run` wrapper to the specified
                       `patch_function`, which automatically creates & terminates an MLflow
                       active run during patch code execution if necessary. If `False`,
                       does not apply the `with_managed_run` wrapper to the specified
                       `patch_function`.
    """
    if manage_run:
        patch_function = with_managed_run(patch_function)

    patch_is_class = inspect.isclass(patch_function)
    if patch_is_class:
        assert issubclass(patch_function, PatchFunction)
    else:
        assert callable(patch_function)

    def safe_patch_function(*args, **kwargs):
        """
        A safe wrapper around the specified `patch_function` implementation designed to
        handle exceptions thrown during the execution of `patch_function`. This wrapper
        distinguishes exceptions thrown from the underlying / original function
        (`<destination>.<function_name>`) from exceptions thrown from other parts of
        `patch_function`. This distinction is made by passing an augmented version of the
        underlying / original function to `patch_function` that uses nonlocal state to track
        whether or not it has been executed and whether or not it threw an exception.

        Exceptions thrown from the underlying / original function are propagated to the caller,
        while exceptions thrown from other parts of `patch_function` are caught and logged as
        warnings.
        """
        original = gorilla.get_original_attribute(destination, function_name)

        # If the autologging integration associated with this patch is disabled,
        # call the original function and return
        if autologging_is_disabled(autologging_integration):
            return original(*args, **kwargs)

        # Whether or not the original / underlying function has been called during the
        # execution of patched code
        original_has_been_called = False
        # The value returned by the call to the original / underlying function during
        # the execution of patched code
        original_result = None
        # Whether or not an exception was raised from within the original / underlying function
        # during the execution of patched code
        failed_during_original = False

        try:

            def call_original(*og_args, **og_kwargs):
                try:
                    if _is_testing():
                        _validate_args(args, kwargs, og_args, og_kwargs)

                    nonlocal original_has_been_called
                    original_has_been_called = True

                    nonlocal original_result
                    original_result = original(*og_args, **og_kwargs)
                    return original_result
                except Exception:  # pylint: disable=broad-except
                    nonlocal failed_during_original
                    failed_during_original = True
                    raise

            # Apply the name, docstring, and signature of `original` to `call_original`.
            # This is important because several autologging patch implementations inspect
            # the signature of the `original` argument during execution
            call_original = _update_wrapper_extended(call_original, original)

            if patch_is_class:
                patch_function.call(call_original, *args, **kwargs)
            else:
                patch_function(call_original, *args, **kwargs)

        except Exception as e:  # pylint: disable=broad-except
            # Exceptions thrown during execution of the original function should be propagated
            # to the caller. Additionally, exceptions encountered during test mode should be
            # reraised to detect bugs in autologging implementations
            if failed_during_original or _is_testing():
                raise

            _logger.warning(
                "Encountered unexpected error during %s autologging: %s", autologging_integration, e
            )

        if original_has_been_called:
            return original_result
        else:
            return original(*args, **kwargs)

    wrap_patch(destination, function_name, safe_patch_function)


def _validate_args(
    user_call_args, user_call_kwargs, autologging_call_args, autologging_call_kwargs
):
    """
    Used for testing purposes to verify that, when a patched ML function calls its underlying
    / original ML function, the following properties are satisfied:

        - All arguments supplied to the patched ML function are forwarded to the
          original ML function
        - Any additional arguments supplied to the original function are exception safe (i.e.
          they are either functions decorated with the `@exception_safe_function` decorator
          or classes / instances of classes with type `ExceptionSafeClass`
    """

    def _validate_new_input(inp):
        """
        Validates a new input (arg or kwarg) introduced to the underlying / original ML function
        call during the execution of a patched ML function. The new input is valid if:

            - The new input is a function that has been decorated with `exception_safe_function`
            - OR the new input is a class with the `ExceptionSafeClass` metaclass
            - OR the new input is a list and each of its elements is valid according to the
              these criteria
        """
        if type(inp) == list:
            for item in inp:
                _validate_new_input(item)
        elif callable(inp):
            assert getattr(inp, _ATTRIBUTE_EXCEPTION_SAFE, False), (
                "New function argument '{}' passed to original function is not exception-safe."
                " Please decorate the function with `exception_safe_function`.".format(inp)
            )
        else:
            assert hasattr(inp, "__class__") and type(inp.__class__) == ExceptionSafeClass, (
                "Invalid new input '{}'. New args / kwargs introduced to `original` function"
                " calls by patched code must either be functions decorated with"
                "`exception_safe_function`, instances of classes with the `ExceptionSafeClass`"
                " metaclass safe or lists of such exception safe functions / classes.".format(inp)
            )

    def _validate(autologging_call_input, user_call_input=None):
        """
        Validates that the specified `autologging_call_input` and `user_call_input`
        are compatible. If `user_call_input` is `None`, then `autologging_call_input`
        is regarded as a new input added by autologging and is validated using
        `_validate_new_input`. Otherwise, the following properties must hold:

            - `autologging_call_input` and `user_call_input` must have the same type
              (referred to as "input type")
            - if the input type is a tuple, list or dictionary, then `autologging_call_input` must
              be equivalent to `user_call_input` or be a superset of `user_call_input`
            - for all other input types, `autologging_call_input` and `user_call_input`
              must be equivalent by reference equality or by object equality
        """
        if user_call_input is None and autologging_call_input is not None:
            _validate_new_input(autologging_call_input)
            return

        assert type(autologging_call_input) == type(
            user_call_input
        ), "Type of input to original function '{}' does not match expected type '{}'".format(
            type(autologging_call_input), type(user_call_input)
        )

        if type(autologging_call_input) in [list, tuple]:
            length_difference = len(autologging_call_input) - len(user_call_input)
            assert length_difference >= 0, (
                "{} expected inputs are missing from the call"
                " to the original function.".format(length_difference)
            )
            # If the autologging call input is longer than the user call input, we `zip_longest`
            # will pad the user call input with `None` values to ensure that the subsequent calls
            # to `_validate` identify new inputs added by the autologging call
            for a, u in itertools.zip_longest(autologging_call_input, user_call_input):
                _validate(a, u)
        elif type(autologging_call_input) == dict:
            assert set(user_call_input.keys()).issubset(set(autologging_call_input.keys())), (
                "Keyword or dictionary arguments to original function omit"
                " one or more expected keys: '{}'".format(
                    set(user_call_input.keys()) - set(autologging_call_input.keys())
                )
            )
            for key in autologging_call_input.keys():
                _validate(autologging_call_input[key], user_call_input.get(key, None))
        else:
            assert (
                autologging_call_input is user_call_input
                or autologging_call_input == user_call_input
            ), (
                "Input to original function does not match expected input."
                " Original: '{}'. Expected: '{}'".format(autologging_call_input, user_call_input)
            )

    _validate(autologging_call_args, user_call_args)
    _validate(autologging_call_kwargs, user_call_kwargs)
