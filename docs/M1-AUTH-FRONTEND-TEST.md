# M1 local Cognito user and Assessment frontend test

M1 sandbox authentication uses the customer-owned Cognito User Pool's local users. The
stack creates `Admin` and `User` groups; it never creates or stores an initial password.
The customer operator creates the controlled test user after the approval-gated stack
deployment and assigns exactly one product group.

## Customer-operated setup

Read the deployed stack outputs `UserPoolId`, `UserPoolClientId`, `CognitoHostedUiDomain`,
and `HttpApiEndpoint`. With customer-approved credentials, create a temporary test user
and add it to `User` (or `Admin` only when administrative behavior is being tested).
Do not record its password or access token in the repository, shell history, or PR.

```bash
aws cognito-idp admin-create-user --user-pool-id '<UserPoolId>' \
  --username '<controlled-email>' --user-attributes Name=email,Value='<controlled-email>'
aws cognito-idp admin-add-user-to-group --user-pool-id '<UserPoolId>' \
  --username '<controlled-email>' --group-name User
```

Use the temporary password delivery and mandatory password-change flow supplied by
Cognito. Customer operators must configure an approved `AssessmentScopeJson` that allows
the repository/profile pair used below.

## Local browser test

The CloudFormation `FrontendCallbackUrl` and `FrontendLogoutUrl` defaults are
`http://localhost:5173`; use those values only for this local sandbox test. Set no secrets:

```bash
export VITE_API_BASE_URL='<HttpApiEndpoint>'
export VITE_COGNITO_DOMAIN='<CognitoHostedUiDomain>'
export VITE_COGNITO_CLIENT_ID='<UserPoolClientId>'
export VITE_COGNITO_REDIRECT_URI='http://localhost:5173'
npm --prefix apps/frontend run dev
```

Open `http://localhost:5173`, choose **Cognito로 로그인**, complete the Hosted UI password
flow, then submit the approved repository ID and policy-profile ID. The SPA exchanges the
authorization code with PKCE, retains the access token only in session storage, calls
`POST /assessments`, and opens the returned report URL.

The stack deployment itself remains an approved GitHub Actions OIDC operation. This guide
does not authorize local AWS deployment, stack mutation, or use of real customer credentials.

For the actual GitHub/AWS/Bedrock M1 evaluation configuration after this login step, use
[M1-SANDBOX-INTEGRATION.md](M1-SANDBOX-INTEGRATION.md).
