# Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Module used to get accounts list from target OUs.
"""

import json
import logging
import os
import boto3
from datetime import date, datetime
from paginator import paginator
from partition import get_partition
from partition import get_organization_api_region


def main():
    accounts = get_accounts()

    account_ids = []
    accounts_from_ous = []
    accounts_from_tags = []

    if TARGET_ACCOUNTS:
        account_ids = [x for x in TARGET_ACCOUNTS.replace(
            '\n', '').replace('\r', '').replace(' ', '').split(',') if x]
        print(f'{TARGET_ACCOUNTS=}')
        print(f'{account_ids=}')
    if TARGET_OUS:
        accounts_from_ous = get_accounts_from_ous()
    if TARGET_TAGS:
        accounts_from_tags = get_accounts_from_tags()

    filtered_ids = set(account_ids + accounts_from_ous + accounts_from_tags)

    with open("target_accounts.json", "w") as a:
        if len(filtered_ids) > 0:
            a.write(json.dumps(
                [account for account in accounts if account["Id"] in filtered_ids], indent=2, default=json_serial))
        else:
            a.write(json.dumps(accounts, indent=2, default=json_serial))


def get_management_account_id():
    # read master_account_id value from ssm parameter store
    if os.environ.get('MANAGEMENT_ACCOUNT_ID'):
        LOGGER.info(
            "MANAGEMENT_ACCOUNT_ID parameter is not needed anymore, you can remove it from the pipeline deployment map")
    ssm = boto3.client('ssm')
    try:
        response = ssm.get_parameter(Name='master_account_id')
    except Exception as e:
        LOGGER.error("Error getting master account id: %s", e)
        raise
    return response["Parameter"]["Value"]


def list_organizational_units_for_parent(parent_ou):
    organizations = get_boto3_client(
        'organizations',
        (
            f'arn:{PARTITION}:sts::{MANAGEMENT_ACCOUNT_ID}:role/'
            f'{CROSS_ACCOUNT_ACCESS_ROLE}-readonly'
        ),
        'getOrganizationUnits',
    )
    organizational_units = [
        ou
        for org_units in (
            organizations
            .get_paginator("list_organizational_units_for_parent")
            .paginate(ParentId=parent_ou)
        )
        for ou in org_units['OrganizationalUnits']
    ]
    return organizational_units


def get_accounts():
    # Return an array of objects like this: [{'AccountId':'xxx','Email':''}]
    LOGGER.info(
        "Management Account ID: %s",
        MANAGEMENT_ACCOUNT_ID
    )
    # Assume a role into the management accounts role to get account ID's
    # and emails
    organizations = get_boto3_client(
        'organizations',
        (
            f'arn:{PARTITION}:sts::{MANAGEMENT_ACCOUNT_ID}:role/'
            f'{CROSS_ACCOUNT_ACCESS_ROLE}-readonly'
        ),
        'getaccountIDs',
    )
    return list(
        map(
            lambda account: account,
            filter(
                lambda account: account['Status'] == 'ACTIVE',
                paginator(organizations.list_accounts)
            )
        )
    )


def get_accounts_from_tags():
    tag_filters = []
    for tags in TARGET_TAGS.split(";"):
        tag_name = tags.split(",", 1)[0].split("=")[1]
        tag_values = tags.split(",", 1)[1].split("=")[1].split(",")
        tag_filters.append({
            "Key": tag_name,
            "Values": tag_values})
    LOGGER.info(
        "Tag filters %s",
        tag_filters
    )
    organization_api_region = get_organization_api_region(REGION_DEFAULT)
    tags_client = get_boto3_client(
        'resourcegroupstaggingapi',
        (
            f'arn:{PARTITION}:sts::{MANAGEMENT_ACCOUNT_ID}:role/'
            f'{CROSS_ACCOUNT_ACCESS_ROLE}-readonly'
        ),
        'getaccountIDsFromTags',
        region_name=organization_api_region,
    )
    account_ids = []
    for resource in paginator(
        tags_client.get_resources,
        TagFilters=tag_filters,
        ResourceTypeFilters=["organizations"],
    ):
        arn = resource["ResourceARN"]
        account_id = arn.split("/")[::-1][0]
        account_ids.append(account_id)
    return account_ids


def get_accounts_from_ous():
    parent_ou_id = None
    account_list = []
    organizations = get_boto3_client(
        'organizations',
        (
            f'arn:{PARTITION}:sts::{MANAGEMENT_ACCOUNT_ID}:role/'
            f'{CROSS_ACCOUNT_ACCESS_ROLE}-readonly'
        ),
        'getRootAccountIDs',
    )
    # Read organization root id
    root_ids = list(
        map(
            lambda root: {'AccountId': root['Id']},
            paginator(organizations.list_roots)
        )
    )
    root_id = root_ids[0]['AccountId']
    for path in TARGET_OUS.split(','):
        # Set initial OU to start looking for given TARGET_OUS
        if parent_ou_id is None:
            parent_ou_id = root_id

        # Parse TARGET_OUS and find the ID
        ou_hierarchy = path.strip('/').split('/')
        hierarchy_index = 0
        if path.strip() == '/':
            account_list.extend(
                get_account_recursive(organizations, parent_ou_id, '/')
            )
        else:
            while hierarchy_index < len(ou_hierarchy):
                org_units = list_organizational_units_for_parent(parent_ou_id)
                for ou in org_units:
                    if ou['Name'] == ou_hierarchy[hierarchy_index]:
                        parent_ou_id = ou['Id']
                        hierarchy_index += 1
                        break
                else:
                    raise ValueError(
                        f'Could not find ou with name {ou_hierarchy} in '
                        f'OU list {org_units}.'
                    )

            account_list.extend(
                get_account_recursive(organizations, parent_ou_id, '/'),
            )
        parent_ou_id = None
    return account_list


def get_boto3_client(service, role, session_name, region_name=''):
    role = sts.assume_role(
        RoleArn=role,
        RoleSessionName=session_name,
        DurationSeconds=900
    )
    session = boto3.Session(
        aws_access_key_id=role['Credentials']['AccessKeyId'],
        aws_secret_access_key=role['Credentials']['SecretAccessKey'],
        aws_session_token=role['Credentials']['SessionToken']
    )
    if region_name != '':
        return session.client(service, region_name=region_name)
    else:
        return session.client(service)


def get_account_recursive(org_client: boto3.client, ou_id: str, path: str) -> list:
    account_list = []
    # Get OUs
    paginator_item = org_client.get_paginator('list_children')
    pages = paginator_item.paginate(
        ParentId=ou_id,
        ChildType='ORGANIZATIONAL_UNIT'
    )
    for page in pages:
        for child in page['Children']:
            account_list.extend(
                get_account_recursive(
                    org_client,
                    child['Id'],
                    f"{path}{ou_id}/",
                )
            )

    # Get Accounts
    pages = paginator_item.paginate(
        ParentId=ou_id,
        ChildType='ACCOUNT'
    )
    for page in pages:
        for child in page['Children']:
            account_list.append(child['Id'])
    return account_list


"""JSON serializer for objects not serializable by default json code"""


def json_serial(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError("Type %s not serializable" % type(obj))


# Configure logging
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(os.environ.get("ADF_LOG_LEVEL", logging.INFO))
logging.basicConfig(level=logging.INFO)

MANAGEMENT_ACCOUNT_ID = get_management_account_id()
TARGET_ACCOUNTS = os.environ.get("TARGET_ACCOUNTS")
TARGET_OUS = os.environ.get("TARGET_OUS")
TARGET_TAGS = os.environ.get("TARGET_TAGS")
REGION_DEFAULT = os.environ["AWS_REGION"]
PARTITION = get_partition(REGION_DEFAULT)
sts = boto3.client('sts')
ssm = boto3.client('ssm')
organizations = boto3.client('organizations')
response = ssm.get_parameter(Name='cross_account_access_role')
CROSS_ACCOUNT_ACCESS_ROLE = response['Parameter']['Value']

if __name__ == "__main__":
    main()
