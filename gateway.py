import os
import  utils
import boto3
from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient
import logging
agent_control = boto3.client(service_name="bedrock-agentcore-control")
s3_client = boto3.client(service_name="s3",region_name=os.getenv("AWS_REGION", "us-east-1"),)

os.environ['AWS_DEFAULT_REGION'] = os.environ.get('AWS_REGION', 'us-east-1')
REGION = os.environ['AWS_DEFAULT_REGION']


client = GatewayClient(region_name=REGION)
client.logger.setLevel(logging.DEBUG)

bucket_name = f'agentcore-gateway-{'us-east-1'}-{REGION}'
file_path = 'openapi-specs/openapi_specification.yaml'
object_key = 'data_for_seo_openapi.json'

def main():
    agentcore_gateway_iam_role = utils.create_agentcore_gateway_role("dfseo_mcp_agentcore_role")
    print("Agentcore gateway role ARN: ", agentcore_gateway_iam_role['Role']['Arn'])

    # create cognito authorizer
    cognito_response = client.create_oauth_authorizer_with_cognito("DFSEOGatewayAuth")
    print(" cognito response", cognito_response["authorizer_config"])

    # create the gateway
    gateway = client.create_mcp_gateway(
        name='DfSEOMCPGATEWAYOPENAPIKEY',  # the name of the Gateway - if you don't set one, one will be generated.
        role_arn=agentcore_gateway_iam_role['Role']['Arn'],  # the role arn that the Gateway will use - if you don't set one, one will be created.
        # Variable from inbound authorization setup steps. Contains the OAuth authorizer details for authorizing callers to your Gateway (MCP only supports OAuth).
        enable_semantic_search=True,  # enable semantic search.

        authorizer_config=cognito_response["authorizer_config"])

    # Get the gateway ID from the gateway object
    gateway_id = gateway.get('gatewayId') or gateway.get('gatewayArn').split('/')[-1]
    print(f"Gateway ID: {gateway_id}")

    # Get the gateway url
    gateway_url = gateway.get('gatewayUrl')
    print(f"Gateway URL: {gateway_url}")

    # Create API KEY Credential Provider

    response = agent_control.create_api_key_credential_provider(
        name="DataForSeoAPIKey",
        apiKey="BASE64ENCODED USERNAME AND PASSWORD FOR DATA4SEO",
    )
    credentialProviderARN = response['credentialProviderArn']

    # Upload the file using put_object and read response


    try:

        s3bucket = s3_client.create_bucket(
                Bucket=bucket_name
            )

        with open(file_path, 'rb') as file_data:
            response = s3_client.put_object(
                Bucket=bucket_name,
                Key=object_key,
                Body=file_data
            )

        # Construct the ARN of the uploaded object with account ID and region
        openapi_s3_uri = f's3://{bucket_name}/{object_key}'
        print(f'Uploaded object S3 URI: {openapi_s3_uri}')
        # S3 Uri for OpenAPI spec file
        data_for_seo_openapi_s3_target_config = {
            "mcp": {
                "openApiSchema": {
                    "s3": {
                        "uri": openapi_s3_uri
                    }
                }
            }
        }

        # API Key credentials provider configuration
        api_key_credential_config = [
            {
                "credentialProviderType": "API_KEY",
                "credentialProvider": {
                    "apiKeyCredentialProvider": {
                        "credentialParameterName": "Authorization",
                        # Replace this with the name of the api key name expected by the respective API provider. For passing token in the header, use "Authorization"
                        "providerArn": credentialProviderARN,
                        "credentialLocation": "HEADER",
                        # Location of api key. Possible values are "HEADER" and "QUERY_PARAMETER".
                        "credentialPrefix": "Basic"
                        # Prefix for the token. Valid values are "Basic". Applies only for tokens.
                    }
                }
            }
        ]

        response = agent_control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name='OpenAPITargetS3DataForSEO',
            description='OpenAPI Target with S3Uri using SDK',
            targetConfiguration=data_for_seo_openapi_s3_target_config,
            credentialProviderConfigurations=api_key_credential_config)


        '''

        # Get access token
        access_token = client.get_access_token_for_cognito(cognito_response["client_info"])

        print("access token:",access_token)
        '''
    except Exception as e:
        print(f'Error uploading file: {e}')


if __name__ == "__main__":
    main()
