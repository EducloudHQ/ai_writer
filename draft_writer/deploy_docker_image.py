import sys
import os
import time

from bedrock_agentcore_starter_toolkit import Runtime
from boto3.session import Session
from utils import create_agentcore_role
boto_session = Session()
region = boto_session.region_name

agentcore_runtime = Runtime()
# Get the current notebook's directory
current_dir = os.path.dirname(os.path.abspath('__file__' if '__file__' in globals() else '.'))

utils_dir = os.path.join(current_dir, '..')
utils_dir = os.path.join(utils_dir, '..')
utils_dir = os.path.abspath(utils_dir)

# Add to sys.path
sys.path.insert(0, utils_dir)
print("sys.path[0]:", sys.path[0])



agent_name="draft_writer_graph_writer"
agentcore_iam_role = create_agentcore_role(agent_name=agent_name)

# generate docker file
response = agentcore_runtime.configure(
    entrypoint="draft_writer_graph_writer.py",
    execution_role=agentcore_iam_role['Role']['Arn'],
    auto_create_ecr=True,
    requirements_file="requirements.txt",
    region=region,
    agent_name=agent_name
)
print(response)

#deploy docker file to agentruntime
launch_result = agentcore_runtime.launch()

# check agent status
status_response = agentcore_runtime.status()
status = status_response.endpoint['status']
end_status = ['READY', 'CREATE_FAILED', 'DELETE_FAILED', 'UPDATE_FAILED']
while status not in end_status:
    time.sleep(10)
    status_response = agentcore_runtime.status()
    status = status_response.endpoint['status']
    print(status)

