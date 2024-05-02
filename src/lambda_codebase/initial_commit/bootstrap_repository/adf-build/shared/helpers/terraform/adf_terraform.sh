#!/usr/bin/env bash

set -euo pipefail

PATH=$PATH:$(pwd)
export PATH
CURRENT=$(pwd)
PARALLELISM="${PARALLELISM:=False}"
TF_VAR_TARGET_ACCOUNT_ROLE="${TF_VAR_TARGET_ACCOUNT_ROLE:=adf-terraform-role}"
RND_BUILD_ID_FILE="${CURRENT}/.rnd_build_id"

function init_variables() {
    # if REGIONS is not defined as pipeline parameters use default region
    if [[ -z "$REGIONS" ]] ; then
        REGIONS=$AWS_DEFAULT_REGION
    fi

    N_ACCOUNTS=$(jq -r '. | length' "${CURRENT}/target_accounts.json")
    ACCOUNTS=$(jq -r '.[].Id' "${CURRENT}/target_accounts.json")

    echo "List of target regions: $REGIONS"
    echo "List of Target accounts (${N_ACCOUNTS}):"
    echo ${ACCOUNTS}
    echo "PARALLEL EXECUTION: ${PARALLELISM}"
    PARALLEL_PARAM=""
    if [[ "${PARALLELISM}" == "true" ]] ; then
        PARALLEL_PARAM="-p"
    fi
}

prepare_workspace() {
    AWS_REGION=$REGION
    # retrieve regional S3 bucket name from parameter store
    S3_BUCKET_REGION_NAME=$(aws ssm get-parameter --name "/cross_region/s3_regional_bucket/$REGION" --region "$AWS_DEFAULT_REGION" | jq .Parameter.Value | sed s/\"//g)
    WORKSPACE_DIR="${CURRENT}/workspaces/${ACCOUNT_NAME}-${REGION}"
    mkdir -p ${WORKSPACE_DIR}
    cd ${WORKSPACE_DIR} || exit
    cp -R "${CURRENT}"/tf/. ${WORKSPACE_DIR}
    # if account related variables exist copy the folder in the work directory
    # we look for folders named after:
    # - the account id (legacy way)
    # - the account name (new way)
    if [ -d "${CURRENT}/tfvars/${ACCOUNT_ID}" ]; then
        cp -R "${CURRENT}/tfvars/${ACCOUNT_ID}/." ${WORKSPACE_DIR}
    fi
    if [ -d "${CURRENT}/tfvars/${ACCOUNT_ID}/${REGION}" ]; then
        cp -R "${CURRENT}/tfvars/${ACCOUNT_ID}/${REGION}"/. ${WORKSPACE_DIR}
    fi
    if [ -d "${CURRENT}/tfvars/${ACCOUNT_NAME}" ]; then
        cp -R "${CURRENT}/tfvars/${ACCOUNT_NAME}/." ${WORKSPACE_DIR}
    fi
    if [ -d "${CURRENT}/tfvars/${ACCOUNT_NAME}/${REGION}" ]; then
        cp -R "${CURRENT}/tfvars/${ACCOUNT_NAME}/${REGION}"/. ${WORKSPACE_DIR}
    fi
    if [ -f "${CURRENT}/tfvars/global.auto.tfvars" ]; then
        cp -R "${CURRENT}/tfvars/global.auto.tfvars" ${WORKSPACE_DIR}
    fi
    cat <<EOF > adf.auto.tfvars
TARGET_ACCOUNT_ID="${ACCOUNT_ID}"
TARGET_REGION="${REGION}"
TARGET_ACCOUNT_ROLE="${TF_VAR_TARGET_ACCOUNT_ROLE}"
EOF
    cat <<EOF > .backend_config
bucket="$S3_BUCKET_REGION_NAME"
region="$REGION" 
key="$ADF_PROJECT_NAME/$ACCOUNT_ID.tfstate"
dynamodb_table="adf-tflocktable"
EOF
    echo "Backend state configuration:"
    cat .backend_config
    echo
    cd ${CURRENT}
}

# adf codebuild deploy pipelines use only the input artifact from the build phase,
# for this reason we need to store the artifact in S3 for the next phases. 
# We store the artifacts in the default region
function store_artifact() {
    ARTIFACT_FILE=artifact_${RND_ID}.tgz
    PLUGIN_CACHE_FILE=plugins_${RND_ID}.tgz
    S3_BUCKET_ARTIFACT=$(aws ssm get-parameter --name "/cross_region/s3_regional_bucket/$AWS_DEFAULT_REGION" --region "$AWS_DEFAULT_REGION" | jq .Parameter.Value | sed s/\"//g)
    echo "Storing artifact ${ARTIFACT_FILE} in s3://$S3_BUCKET_ARTIFACT/$ADF_PROJECT_NAME/artifacts"
    C=$(pwd)
    cd ${CURRENT}
    tar -N $RND_BUILD_ID_FILE -czf /tmp/$ARTIFACT_FILE .
    aws s3 cp --only-show-errors /tmp/$ARTIFACT_FILE s3://$S3_BUCKET_ARTIFACT/$ADF_PROJECT_NAME/artifacts/$ARTIFACT_FILE --region $AWS_DEFAULT_REGION
    cd /tmp
    tar -czf /tmp/$PLUGIN_CACHE_FILE .terraform.d
    aws s3 cp --only-show-errors /tmp/$PLUGIN_CACHE_FILE s3://$S3_BUCKET_ARTIFACT/$ADF_PROJECT_NAME/artifacts/$PLUGIN_CACHE_FILE --region $AWS_DEFAULT_REGION
    cd $C
}

function restore_artifact() {
    ARTIFACT_FILE=artifact_${RND_ID}.tgz
    PLUGIN_CACHE_FILE=plugins_${RND_ID}.tgz
    S3_BUCKET_ARTIFACT=$(aws ssm get-parameter --name "/cross_region/s3_regional_bucket/$AWS_DEFAULT_REGION" --region "$AWS_DEFAULT_REGION" | jq .Parameter.Value | sed s/\"//g)
    set +e
    aws s3 ls s3://$S3_BUCKET_ARTIFACT/$ADF_PROJECT_NAME/artifacts/$ARTIFACT_FILE --region $AWS_DEFAULT_REGION
    ERR_CODE=$?
    set -e
    if [ $ERR_CODE -ne 0 ]; then
        echo "No artifact $ARTIFACT_FILE found in s3://$S3_BUCKET_ARTIFACT/$ADF_PROJECT_NAME/artifacts"
        echo "Proceeding with terraform init"
        return
    fi
    C=$(pwd)
    cd ${CURRENT}
    echo "Restoring artifact ${ARTIFACT_FILE} from s3://$S3_BUCKET_ARTIFACT/$ADF_PROJECT_NAME/artifacts"
    aws s3 --only-show-errors cp s3://$S3_BUCKET_ARTIFACT/$ADF_PROJECT_NAME/artifacts/$ARTIFACT_FILE $ARTIFACT_FILE --region $AWS_DEFAULT_REGION
    tar xzf $ARTIFACT_FILE
    cd /tmp
    aws s3 --only-show-errors cp s3://$S3_BUCKET_ARTIFACT/$ADF_PROJECT_NAME/artifacts/$PLUGIN_CACHE_FILE $PLUGIN_CACHE_FILE --region $AWS_DEFAULT_REGION
    tar xzf $PLUGIN_CACHE_FILE
    cd $C
}

# at the moment ADF uses codepipeline V1 that doesn't have pipeline variables, 
# we generate a random id in install phase that we can use as identifier in the next steps
function get_build_id() {
    # get the build id from the artifact file, if it exists
    if [[ -e ${RND_BUILD_ID_FILE} ]] ; then    
        RND_ID=$(cat ${RND_BUILD_ID_FILE})
    else
        # otherwise create a random id
        RND_ID=$(head /dev/urandom | LC_ALL=C tr -dc A-Za-z0-9 | head -c 6)        
        echo $RND_ID > ${RND_BUILD_ID_FILE}
    fi
    echo "Build id: $RND_ID"
}

function tfinstall() {
    python adf-build/helpers/terraform/adf-parallel-terraform.py -s install -p -t ${TERRAFORM_VERSION} -w workspaces/*
    get_build_id
}

function tfinit() {
    # get_build_id
    init_variables
    echo
    for REGION in $(echo "$REGIONS" | sed "s/,/ /g") ; do
        S3_BUCKET_REGION_NAME=$(aws ssm get-parameter --name "/cross_region/s3_regional_bucket/$REGION" --region "$AWS_DEFAULT_REGION" | jq .Parameter.Value | sed s/\"//g)

        for ACCOUNT_ID in $ACCOUNTS ; do
            ACCOUNT_NAME=$(jq -r ".[] | select(.Id==\"${ACCOUNT_ID}\") | .Name" "${CURRENT}/target_accounts.json")
            echo "Preparing workspace for ${ACCOUNT_NAME} (${ACCOUNT_ID}) in $REGION"
            prepare_workspace
        done
    done
    # we create a symlink named "tmp" to workspaces folder for backward compatibility
    ln -s workspaces tmp
    
    python adf-build/helpers/terraform/adf-parallel-terraform.py ${PARALLEL_PARAM} -s init -w workspaces/* -b .backend_config
    touch .init.done
}
function tfplan() {
    # you can call directly plan, the script will take care of calling init if you didn't
    if [ ! -f .init.done ]; then
        tfinit
    fi
    init_variables
    echo
    python adf-build/helpers/terraform/adf-parallel-terraform.py ${PARALLEL_PARAM} -s plan -w workspaces/*
    touch .plan.done
    # store the artifact in S3 for the next phases, using the build id as identifier
    get_build_id
    store_artifact
}
function tfapply() {
    # restore the artifact from S3, using the build id as identifier
    get_build_id
    restore_artifact
    # you can call directly apply, the script will take care of calling init if you didn't
    if [ ! -f .plan.done ] ; then
        tfplan
    fi
    init_variables
    python adf-build/helpers/terraform/adf-parallel-terraform.py ${PARALLEL_PARAM} -s apply -w workspaces/*
}
function tfplandestroy() {
    echo "plan destroy"
    # terraform plan -destroy -out "${ADF_PROJECT_NAME}-${TF_VAR_TARGET_ACCOUNT_ID}-destroy"
}
function tfdestroy() {
    echo "destroy"
    # terraform apply "${ADF_PROJECT_NAME}-${TF_VAR_TARGET_ACCOUNT_ID}-destroy"
}

function tfrun() {
    if [[ "$TF_STAGE" == "install" ]] ; then
        tfinstall
    elif [[ "$TF_STAGE" == "init" ]] ; then
        tfinit
    elif [[ "$TF_STAGE" == "plan" ]] ; then
        tfplan
    elif [[ "$TF_STAGE" == "apply" ]] ; then
        tfapply
    elif [[ "$TF_STAGE" == "destroy" ]] ; then
        tfplandestroy
        tfdestroy
    else
        echo "Invalid Terraform stage: TF_STAGE = $TF_STAGE"
        exit 1
    fi
}

tfrun