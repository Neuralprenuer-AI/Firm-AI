import sys
import os
import importlib.util

_BASE = os.path.join(os.path.dirname(__file__), '..')

def load_lambda(name: str):
    """Load a Lambda's lambda_function.py by exact path, bypassing sys.path ordering."""
    sys.modules.pop('lambda_function', None)
    path = os.path.abspath(os.path.join(_BASE, 'lambdas', name, 'lambda_function.py'))
    spec = importlib.util.spec_from_file_location('lambda_function', path)
    lf = importlib.util.module_from_spec(spec)
    sys.modules['lambda_function'] = lf
    spec.loader.exec_module(lf)
    return lf
