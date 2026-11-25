#define _POSIX_C_SOURCE 200809L
#include <openssl/core_dispatch.h>
#include <openssl/core_names.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/params.h>
#include <openssl/provider.h>
#include <openssl/rand.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>
#include <openssl/err.h>
#include <string.h>
#include <errno.h>
#include <poll.h>
#include <time.h>

#define PROVIDER "vp-retention-signer"
#define DEFAULT "provider=default"
#define SIGNER "provider=" PROVIDER
#define SIGNER_PARAM "retention-signer-callback"
#define POINT_SIZE 65
#define SIGNATURE_SIZE 72
#define HOST_FLAGS (X509_CHECK_FLAG_NEVER_CHECK_SUBJECT | X509_CHECK_FLAG_NO_WILDCARDS)
#define KEY_PUBLIC OSSL_KEYMGMT_SELECT_PUBLIC_KEY
#define KEY_PRIVATE OSSL_KEYMGMT_SELECT_PRIVATE_KEY

typedef int (*sign_callback)(const unsigned char *, unsigned char *, size_t *);

typedef struct {
    unsigned char point[POINT_SIZE];
    int has_point;
    sign_callback sign;
} external_key;

static void *key_new(void *provider)
{
    (void)provider;
    return OPENSSL_zalloc(sizeof(external_key));
}

static void key_free(void *key)
{
    OPENSSL_free(key);
}

static void *key_dup(const void *data, int selection)
{
    const external_key *source = data;
    external_key *key = key_new(NULL);
    if (key != NULL) {
        memcpy(key, source, sizeof(*key));
        if (!(selection & KEY_PRIVATE))
            key->sign = NULL;
        if (!(selection & KEY_PUBLIC))
            key->has_point = 0;
    }
    return key;
}

static int group_is_p256(const char *name)
{
    return OPENSSL_strcasecmp(name, "prime256v1") == 0 ||
           OPENSSL_strcasecmp(name, "secp256r1") == 0 ||
           OPENSSL_strcasecmp(name, "P-256") == 0;
}

static int key_import(void *data, int selection, const OSSL_PARAM params[])
{
    external_key *key = data;
    const OSSL_PARAM *group = OSSL_PARAM_locate_const(params, OSSL_PKEY_PARAM_GROUP_NAME);
    const OSSL_PARAM *point = OSSL_PARAM_locate_const(params, OSSL_PKEY_PARAM_PUB_KEY);
    const OSSL_PARAM *signer = OSSL_PARAM_locate_const(params, SIGNER_PARAM);
    const char *name = NULL;
    if (group == NULL || !OSSL_PARAM_get_utf8_string_ptr(group, &name) || !group_is_p256(name))
        return 0;
    if (selection & KEY_PUBLIC) {
        if (point == NULL || point->data_type != OSSL_PARAM_OCTET_STRING ||
            point->data_size != POINT_SIZE || ((unsigned char *)point->data)[0] != 4)
            return 0;
        memcpy(key->point, point->data, POINT_SIZE);
        key->has_point = 1;
    }
    if (selection & KEY_PRIVATE) {
        if (signer == NULL || signer->data_type != OSSL_PARAM_OCTET_STRING ||
            signer->data_size != sizeof(key->sign))
            return 0;
        memcpy(&key->sign, signer->data, sizeof(key->sign));
        if (key->sign == NULL)
            return 0;
    }
    return 1;
}

static const OSSL_PARAM public_types[] = {
    OSSL_PARAM_utf8_string(OSSL_PKEY_PARAM_GROUP_NAME, NULL, 0),
    OSSL_PARAM_octet_string(OSSL_PKEY_PARAM_PUB_KEY, NULL, 0),
    OSSL_PARAM_END
};

static const OSSL_PARAM external_types[] = {
    OSSL_PARAM_utf8_string(OSSL_PKEY_PARAM_GROUP_NAME, NULL, 0),
    OSSL_PARAM_octet_string(OSSL_PKEY_PARAM_PUB_KEY, NULL, 0),
    OSSL_PARAM_octet_string(SIGNER_PARAM, NULL, 0),
    OSSL_PARAM_END
};

static const OSSL_PARAM *key_import_types(int selection)
{
    return selection & KEY_PRIVATE ? external_types : public_types;
}

static const OSSL_PARAM *key_export_types(int selection)
{
    return selection & KEY_PRIVATE ? NULL : public_types;
}

static int key_export(void *data, int selection, OSSL_CALLBACK *emit, void *arg)
{
    external_key *key = data;
    char group[] = "prime256v1";
    OSSL_PARAM params[] = {
        OSSL_PARAM_construct_utf8_string(OSSL_PKEY_PARAM_GROUP_NAME, group, 0),
        OSSL_PARAM_END, OSSL_PARAM_END
    };
    if (selection & KEY_PRIVATE)
        return 0;
    if (selection & KEY_PUBLIC) {
        if (!key->has_point)
            return 0;
        params[1] = OSSL_PARAM_construct_octet_string(OSSL_PKEY_PARAM_PUB_KEY,
                                                      key->point, POINT_SIZE);
    }
    return emit(params, arg);
}

static int key_has(const void *data, int selection)
{
    const external_key *key = data;
    return (!(selection & KEY_PUBLIC) || key->has_point) &&
           (!(selection & KEY_PRIVATE) || key->sign != NULL);
}

static int key_match(const void *left, const void *right, int selection)
{
    const external_key *a = left, *b = right;
    return key_has(a, selection) && key_has(b, selection) &&
           (!(selection & KEY_PUBLIC) || CRYPTO_memcmp(a->point, b->point, POINT_SIZE) == 0) &&
           (!(selection & KEY_PRIVATE) || a->sign == b->sign);
}

static const OSSL_PARAM key_params[] = {
    OSSL_PARAM_int(OSSL_PKEY_PARAM_BITS, NULL),
    OSSL_PARAM_int(OSSL_PKEY_PARAM_SECURITY_BITS, NULL),
    OSSL_PARAM_int(OSSL_PKEY_PARAM_MAX_SIZE, NULL),
    OSSL_PARAM_utf8_string(OSSL_PKEY_PARAM_GROUP_NAME, NULL, 0),
    OSSL_PARAM_utf8_string(OSSL_PKEY_PARAM_DEFAULT_DIGEST, NULL, 0),
    OSSL_PARAM_octet_string(OSSL_PKEY_PARAM_ENCODED_PUBLIC_KEY, NULL, 0),
    OSSL_PARAM_END
};

static const OSSL_PARAM *key_gettable(void *provider)
{
    (void)provider;
    return key_params;
}

static int key_get(void *data, OSSL_PARAM params[])
{
    const external_key *key = data;
    OSSL_PARAM *param;
    if ((param = OSSL_PARAM_locate(params, OSSL_PKEY_PARAM_BITS)) && !OSSL_PARAM_set_int(param, 256))
        return 0;
    if ((param = OSSL_PARAM_locate(params, OSSL_PKEY_PARAM_SECURITY_BITS)) && !OSSL_PARAM_set_int(param, 128))
        return 0;
    if ((param = OSSL_PARAM_locate(params, OSSL_PKEY_PARAM_MAX_SIZE)) && !OSSL_PARAM_set_int(param, SIGNATURE_SIZE))
        return 0;
    if ((param = OSSL_PARAM_locate(params, OSSL_PKEY_PARAM_GROUP_NAME)) && !OSSL_PARAM_set_utf8_string(param, "prime256v1"))
        return 0;
    if ((param = OSSL_PARAM_locate(params, OSSL_PKEY_PARAM_DEFAULT_DIGEST)) && !OSSL_PARAM_set_utf8_string(param, "SHA256"))
        return 0;
    if ((param = OSSL_PARAM_locate(params, OSSL_PKEY_PARAM_ENCODED_PUBLIC_KEY)) &&
        (!key->has_point || !OSSL_PARAM_set_octet_string(param, key->point, POINT_SIZE)))
        return 0;
    return 1;
}

static const char *key_operation(int operation)
{
    return operation == OSSL_OP_SIGNATURE ? "ECDSA" : NULL;
}

typedef struct {
    const external_key *key;
    EVP_MD_CTX *hash;
} signature_ctx;

static void *signature_new(void *provider, const char *properties)
{
    (void)provider;
    (void)properties;
    return OPENSSL_zalloc(sizeof(signature_ctx));
}

static void signature_free(void *data)
{
    signature_ctx *ctx = data;
    if (ctx != NULL) {
        EVP_MD_CTX_free(ctx->hash);
        OPENSSL_free(ctx);
    }
}

static void *signature_dup(void *data)
{
    const signature_ctx *source = data;
    signature_ctx *ctx = signature_new(NULL, NULL);
    if (ctx == NULL)
        return NULL;
    ctx->key = source->key;
    if (source->hash != NULL) {
        ctx->hash = EVP_MD_CTX_new();
        if (ctx->hash == NULL || EVP_MD_CTX_copy_ex(ctx->hash, source->hash) != 1) {
            signature_free(ctx);
            return NULL;
        }
    }
    return ctx;
}

static int sha256_name(const char *name)
{
    return OPENSSL_strcasecmp(name, "SHA256") == 0 ||
           OPENSSL_strcasecmp(name, "SHA2-256") == 0 ||
           OPENSSL_strcasecmp(name, "SHA-256") == 0;
}

static int signature_set(void *data, const OSSL_PARAM params[])
{
    (void)data;
    if (params == NULL)
        return 1;
    const OSSL_PARAM *digest = OSSL_PARAM_locate_const(params, OSSL_SIGNATURE_PARAM_DIGEST);
    const OSSL_PARAM *size = OSSL_PARAM_locate_const(params, OSSL_SIGNATURE_PARAM_DIGEST_SIZE);
    const char *name;
    size_t bytes;
    return (digest == NULL || (OSSL_PARAM_get_utf8_string_ptr(digest, &name) && sha256_name(name))) &&
           (size == NULL || (OSSL_PARAM_get_size_t(size, &bytes) && bytes == 32));
}

static const OSSL_PARAM *signature_settable(void *data, void *provider)
{
    (void)data;
    (void)provider;
    static const OSSL_PARAM params[] = {
        OSSL_PARAM_utf8_string(OSSL_SIGNATURE_PARAM_DIGEST, NULL, 0),
        OSSL_PARAM_size_t(OSSL_SIGNATURE_PARAM_DIGEST_SIZE, NULL),
        OSSL_PARAM_END
    };
    return params;
}

static int sign_init(void *data, void *key, const OSSL_PARAM params[])
{
    signature_ctx *ctx = data;
    ctx->key = key;
    return key != NULL && ctx->key->sign != NULL && signature_set(ctx, params);
}

static int sign_digest(void *data, unsigned char *out, size_t *outlen, size_t capacity,
                       const unsigned char *digest, size_t digestlen)
{
    signature_ctx *ctx = data;
    if (out == NULL) {
        *outlen = SIGNATURE_SIZE;
        return 1;
    }
    if (capacity < SIGNATURE_SIZE || digestlen != 32 || ctx->key == NULL)
        return 0;
    *outlen = SIGNATURE_SIZE;
    return ctx->key->sign(digest, out, outlen) == 1 && *outlen <= SIGNATURE_SIZE;
}

static int digest_init(void *data, const char *name, void *key, const OSSL_PARAM params[])
{
    signature_ctx *ctx = data;
    if (name == NULL || !sha256_name(name) || !sign_init(ctx, key, params))
        return 0;
    if (ctx->hash == NULL)
        ctx->hash = EVP_MD_CTX_new();
    EVP_MD *digest = EVP_MD_fetch(NULL, "SHA256", DEFAULT);
    int ok = ctx->hash != NULL && digest != NULL && EVP_DigestInit_ex(ctx->hash, digest, NULL) == 1;
    EVP_MD_free(digest);
    return ok;
}

static int digest_update(void *data, const unsigned char *message, size_t length)
{
    signature_ctx *ctx = data;
    return ctx->hash != NULL && EVP_DigestUpdate(ctx->hash, message, length);
}

static int digest_final(void *data, unsigned char *out, size_t *outlen, size_t capacity)
{
    signature_ctx *ctx = data;
    unsigned char digest[32];
    unsigned int bytes;
    if (out == NULL) {
        *outlen = SIGNATURE_SIZE;
        return 1;
    }
    return ctx->hash != NULL && EVP_DigestFinal_ex(ctx->hash, digest, &bytes) == 1 &&
           sign_digest(ctx, out, outlen, capacity, digest, bytes);
}

static int digest_once(void *data, unsigned char *out, size_t *outlen, size_t capacity,
                       const unsigned char *message, size_t length)
{
    unsigned char digest[32];
    size_t bytes;
    if (out == NULL) {
        *outlen = SIGNATURE_SIZE;
        return 1;
    }
    return EVP_Q_digest(NULL, "SHA256", DEFAULT, message, length, digest, &bytes) == 1 &&
           sign_digest(data, out, outlen, capacity, digest, bytes);
}

#define IMPLEMENT(number, name) { number, (void (*)(void))name }
static const OSSL_DISPATCH keys[] = {
    IMPLEMENT(OSSL_FUNC_KEYMGMT_NEW, key_new),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_FREE, key_free),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_DUP, key_dup),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_IMPORT, key_import),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_IMPORT_TYPES, key_import_types),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_EXPORT, key_export),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_EXPORT_TYPES, key_export_types),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_HAS, key_has),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_MATCH, key_match),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_GET_PARAMS, key_get),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_GETTABLE_PARAMS, key_gettable),
    IMPLEMENT(OSSL_FUNC_KEYMGMT_QUERY_OPERATION_NAME, key_operation),
    { 0, NULL }
};

static const OSSL_DISPATCH signatures[] = {
    IMPLEMENT(OSSL_FUNC_SIGNATURE_NEWCTX, signature_new),
    IMPLEMENT(OSSL_FUNC_SIGNATURE_FREECTX, signature_free),
    IMPLEMENT(OSSL_FUNC_SIGNATURE_DUPCTX, signature_dup),
    IMPLEMENT(OSSL_FUNC_SIGNATURE_SIGN_INIT, sign_init),
    IMPLEMENT(OSSL_FUNC_SIGNATURE_SIGN, sign_digest),
    IMPLEMENT(OSSL_FUNC_SIGNATURE_DIGEST_SIGN_INIT, digest_init),
    IMPLEMENT(OSSL_FUNC_SIGNATURE_DIGEST_SIGN_UPDATE, digest_update),
    IMPLEMENT(OSSL_FUNC_SIGNATURE_DIGEST_SIGN_FINAL, digest_final),
    IMPLEMENT(OSSL_FUNC_SIGNATURE_DIGEST_SIGN, digest_once),
    IMPLEMENT(OSSL_FUNC_SIGNATURE_SET_CTX_PARAMS, signature_set),
    IMPLEMENT(OSSL_FUNC_SIGNATURE_SETTABLE_CTX_PARAMS, signature_settable),
    { 0, NULL }
};

static const OSSL_ALGORITHM *query(void *context, int operation, int *no_cache)
{
    (void)context;
    static const OSSL_ALGORITHM key_algs[] = {
        { "EC:id-ecPublicKey:1.2.840.10045.2.1", SIGNER, keys, NULL }, { NULL, NULL, NULL, NULL }
    };
    static const OSSL_ALGORITHM signature_algs[] = {
        { "ECDSA", SIGNER, signatures, NULL }, { NULL, NULL, NULL, NULL }
    };
    *no_cache = 0;
    switch (operation) {
    case OSSL_OP_KEYMGMT: return key_algs;
    case OSSL_OP_SIGNATURE: return signature_algs;
    default: return NULL;
    }
}

static int provider_init(const OSSL_CORE_HANDLE *handle, const OSSL_DISPATCH *core,
                         const OSSL_DISPATCH **output, void **context)
{
    (void)handle;
    (void)core;
    static const OSSL_DISPATCH dispatch[] = {
        IMPLEMENT(OSSL_FUNC_PROVIDER_QUERY_OPERATION, query), { 0, NULL }
    };
    *context = NULL;
    *output = dispatch;
    return 1;
}

static CRYPTO_ONCE once = CRYPTO_ONCE_STATIC_INIT;
static int provider_ready;

static void register_provider(void)
{
    /* The library and provider are retained for the lifetime of this process. */
    provider_ready = OSSL_PROVIDER_add_builtin(NULL, PROVIDER, provider_init) == 1 &&
                     OSSL_PROVIDER_load(NULL, "default") != NULL &&
                     OSSL_PROVIDER_load(NULL, PROVIDER) != NULL;
}

static EVP_PKEY *external_pkey(EVP_PKEY *public, sign_callback sign)
{
    char group[32];
    unsigned char point[POINT_SIZE];
    size_t length;
    if (!EVP_PKEY_is_a(public, "EC") ||
        !EVP_PKEY_get_group_name(public, group, sizeof(group), NULL) || !group_is_p256(group) ||
        !EVP_PKEY_get_octet_string_param(public, OSSL_PKEY_PARAM_PUB_KEY, point, sizeof(point), &length) ||
        length != sizeof(point))
        return NULL;
    OSSL_PARAM params[] = {
        OSSL_PARAM_construct_utf8_string(OSSL_PKEY_PARAM_GROUP_NAME, group, 0),
        OSSL_PARAM_construct_octet_string(OSSL_PKEY_PARAM_PUB_KEY, point, length),
        OSSL_PARAM_construct_octet_string(SIGNER_PARAM, &sign, sizeof(sign)),
        OSSL_PARAM_construct_end()
    };
    EVP_PKEY_CTX *context = EVP_PKEY_CTX_new_from_name(NULL, "EC", SIGNER);
    EVP_PKEY *private = NULL;
    if (context != NULL && EVP_PKEY_fromdata_init(context) == 1)
        if (EVP_PKEY_fromdata(context, &private, EVP_PKEY_KEYPAIR, params) != 1) {
            EVP_PKEY_free(private);
            private = NULL;
        }
    EVP_PKEY_CTX_free(context);
    return private;
}

static int export_is_public_only(EVP_PKEY *key)
{
    OSSL_PARAM *params = NULL;
    int public_ok = EVP_PKEY_todata(key, EVP_PKEY_PUBLIC_KEY, &params) == 1;
    OSSL_PARAM_free(params);
    params = NULL;
    int private_denied = EVP_PKEY_todata(key, EVP_PKEY_PRIVATE_KEY, &params) != 1;
    OSSL_PARAM_free(params);
    return public_ok && private_denied;
}

static int challenge_key(EVP_PKEY *public, EVP_PKEY *private)
{
    unsigned char challenge[32], signature[SIGNATURE_SIZE];
    size_t length = sizeof(signature);
    EVP_MD_CTX *sign = EVP_MD_CTX_new(), *verify = EVP_MD_CTX_new();
    int ok = sign != NULL && verify != NULL && RAND_bytes(challenge, sizeof(challenge)) == 1 &&
             EVP_DigestSignInit_ex(sign, NULL, "SHA256", NULL, SIGNER, private, NULL) == 1 &&
             EVP_DigestSignUpdate(sign, challenge, sizeof(challenge)) == 1 &&
             EVP_DigestSignFinal(sign, NULL, &length) == 1 && length == sizeof(signature) &&
             EVP_DigestSignFinal(sign, signature, &length) == 1 &&
             EVP_DigestVerifyInit_ex(verify, NULL, "SHA256", NULL, DEFAULT, public, NULL) == 1 &&
             EVP_DigestVerify(verify, signature, length, challenge, sizeof(challenge)) == 1;
    EVP_MD_CTX_free(sign);
    EVP_MD_CTX_free(verify);
    return ok;
}

static int explicit_eku(X509 *cert, int purpose)
{
    EXTENDED_KEY_USAGE *eku = X509_get_ext_d2i(cert, NID_ext_key_usage, NULL, NULL);
    int match = 0;
    for (int i = 0; i < sk_ASN1_OBJECT_num(eku); i++)
        if (OBJ_obj2nid(sk_ASN1_OBJECT_value(eku, i)) == purpose)
            match = 1;
    EXTENDED_KEY_USAGE_free(eku);
    return match;
}

static X509_STORE *trust_store(const char *bundle)
{
    X509_STORE *trust = X509_STORE_new();
    if (trust != NULL && X509_STORE_load_file(trust, bundle) == 1 &&
        X509_STORE_set_flags(trust, X509_V_FLAG_CRL_CHECK | X509_V_FLAG_CRL_CHECK_ALL) == 1)
        return trust;
    X509_STORE_free(trust);
    return NULL;
}

static int verify_identity(X509 *leaf, STACK_OF(X509) *chain, const char *trust,
                           const char *hostname, int for_server)
{
    X509_STORE *store = trust_store(trust);
    X509_STORE_CTX *ctx = X509_STORE_CTX_new();
    int ok = leaf != NULL && store != NULL && ctx != NULL &&
             X509_STORE_CTX_init(ctx, store, leaf, chain) == 1 &&
             X509_STORE_CTX_set_purpose(ctx, for_server ? X509_PURPOSE_SSL_SERVER : X509_PURPOSE_SSL_CLIENT) == 1 &&
             explicit_eku(leaf, for_server ? NID_server_auth : NID_client_auth);
    if (ok && for_server) {
        X509_VERIFY_PARAM *parameters = X509_STORE_CTX_get0_param(ctx);
        X509_VERIFY_PARAM_set_hostflags(parameters, HOST_FLAGS);
        ok = X509_VERIFY_PARAM_set1_host(parameters, hostname, 0) == 1;
    }
    ok = ok && X509_verify_cert(ctx) == 1;
    X509_STORE_CTX_free(ctx);
    X509_STORE_free(store);
    return ok;
}

int vp_own_identity(SSL_CTX *ctx, const char *trust, const char *hostname, int server)
{
    ERR_clear_error();
    X509 *leaf = SSL_CTX_get0_certificate(ctx);
    STACK_OF(X509) *chain = NULL;
    SSL_CTX_get0_chain_certs(ctx, &chain);
    return verify_identity(leaf, chain, trust, hostname, server) &&
           challenge_key(X509_get0_pubkey(leaf), SSL_CTX_get0_privatekey(ctx));
}

SSL_CTX *vp_context(const char *chain, const char *trust, const char *hostname,
                    int server, sign_callback sign)
{
    ERR_clear_error();
    if (!CRYPTO_THREAD_run_once(&once, register_provider) || !provider_ready)
        return NULL;
    SSL_CTX *ctx = SSL_CTX_new(TLS_method());
    if (ctx == NULL)
        return NULL;
    X509_STORE *store = trust_store(trust);
    if (store == NULL)
        goto fail;
    SSL_CTX_set_cert_store(ctx, store);
    SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER | (server ? SSL_VERIFY_FAIL_IF_NO_PEER_CERT : 0), NULL);
    SSL_CTX_set_options(ctx, SSL_OP_NO_TICKET | SSL_OP_NO_RENEGOTIATION);
    SSL_CTX_set_session_cache_mode(ctx, SSL_SESS_CACHE_OFF);
    SSL_CTX_set_keylog_callback(ctx, NULL);
    if (SSL_CTX_set_min_proto_version(ctx, TLS1_3_VERSION) != 1 ||
        SSL_CTX_set_max_proto_version(ctx, TLS1_3_VERSION) != 1 ||
        SSL_CTX_set_num_tickets(ctx, 0) != 1 ||
        SSL_CTX_set_max_early_data(ctx, 0) != 1 ||
        SSL_CTX_set1_sigalgs_list(ctx, "ecdsa_secp256r1_sha256") != 1 ||
        SSL_CTX_use_certificate_chain_file(ctx, chain) != 1)
        goto fail;
    X509 *leaf = SSL_CTX_get0_certificate(ctx);
    EVP_PKEY *public = leaf == NULL ? NULL : X509_get0_pubkey(leaf);
    EVP_PKEY *key = public == NULL ? NULL : external_pkey(public, sign);
    int ok = key != NULL && export_is_public_only(key) && SSL_CTX_use_PrivateKey(ctx, key) == 1 &&
             SSL_CTX_check_private_key(ctx) == 1 && vp_own_identity(ctx, trust, hostname, server);
    EVP_PKEY_free(key);
    if (ok)
        return ctx;
fail:
    SSL_CTX_free(ctx);
    return NULL;
}

void vp_context_free(SSL_CTX *ctx) { SSL_CTX_free(ctx); }

static long long monotonic_ms(void)
{
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time) != 0)
        return -1;
    return (long long)time.tv_sec * 1000 + time.tv_nsec / 1000000;
}

static int wait_io(SSL *ssl, int result, long long deadline)
{
    int error = SSL_get_error(ssl, result);
    if (error != SSL_ERROR_WANT_READ && error != SSL_ERROR_WANT_WRITE)
        return 0;
    struct pollfd fd = { SSL_get_fd(ssl), error == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT, 0 };
    for (;;) {
        long long now = monotonic_ms();
        if (now < 0 || deadline <= now)
            return 0;
        int ready = poll(&fd, 1, (int)(deadline - now));
        if (ready >= 0 || errno != EINTR)
            return ready > 0 && !(fd.revents & POLLNVAL);
    }
}

SSL *vp_connect(SSL_CTX *context, int fd, const char *trust, const char *hostname,
                int server, int timeout_ms)
{
    ERR_clear_error();
    SSL *ssl = SSL_new(context);
    if (ssl == NULL)
        return NULL;
    X509_STORE *store = trust_store(trust);
    int trust_ok = store != NULL && SSL_set1_verify_cert_store(ssl, store) == 1;
    X509_STORE_free(store);
    if (!trust_ok || SSL_set_fd(ssl, fd) != 1)
        goto fail;
    if (!server) {
        SSL_set_hostflags(ssl, HOST_FLAGS);
        if (SSL_set1_host(ssl, hostname) != 1 || SSL_set_tlsext_host_name(ssl, hostname) != 1)
            goto fail;
    }
    long long deadline = monotonic_ms() + timeout_ms;
    int result;
    do {
        result = server ? SSL_accept(ssl) : SSL_connect(ssl);
    } while (result != 1 && wait_io(ssl, result, deadline));
    if (result == 1 && SSL_get_verify_result(ssl) == X509_V_OK && !SSL_session_reused(ssl)) {
        X509 *leaf = SSL_get0_peer_certificate(ssl);
        if (leaf != NULL && explicit_eku(leaf, server ? NID_client_auth : NID_server_auth))
            return ssl;
    }
fail:
    SSL_free(ssl);
    return NULL;
}

int vp_current(SSL *ssl, const char *trust, const char *hostname, int server)
{
    return verify_identity(SSL_get0_peer_certificate(ssl), SSL_get_peer_cert_chain(ssl),
                           trust, hostname, !server);
}

int vp_peer_digest(SSL *ssl, unsigned char *digest)
{
    X509 *leaf = SSL_get0_peer_certificate(ssl);
    unsigned int length;
    return leaf != NULL && X509_digest(leaf, EVP_sha256(), digest, &length) == 1 && length == 32;
}

int vp_peer_uri(SSL *ssl, const char *uri, size_t length)
{
    X509 *leaf = SSL_get0_peer_certificate(ssl);
    if (leaf == NULL)
        return 0;
    GENERAL_NAMES *names = X509_get_ext_d2i(leaf, NID_subject_alt_name, NULL, NULL);
    int match = 0;
    for (int i = 0; i < sk_GENERAL_NAME_num(names); i++) {
        GENERAL_NAME *name = sk_GENERAL_NAME_value(names, i);
        if (name->type == GEN_URI && (size_t)ASN1_STRING_length(name->d.uniformResourceIdentifier) == length &&
            memcmp(ASN1_STRING_get0_data(name->d.uniformResourceIdentifier), uri, length) == 0)
            match = 1;
    }
    GENERAL_NAMES_free(names);
    return match;
}

int vp_read(SSL *ssl, unsigned char *buffer, int length, int timeout_ms)
{
    ERR_clear_error();
    long long deadline = monotonic_ms() + timeout_ms;
    int result;
    do {
        result = SSL_read(ssl, buffer, length);
    } while (result <= 0 && wait_io(ssl, result, deadline));
    if (result > 0)
        return result;
    return SSL_get_error(ssl, result) == SSL_ERROR_ZERO_RETURN ? 0 : -1;
}

int vp_write(SSL *ssl, const unsigned char *buffer, int length, int timeout_ms)
{
    ERR_clear_error();
    long long deadline = monotonic_ms() + timeout_ms;
    int result;
    do {
        result = SSL_write(ssl, buffer, length);
    } while (result <= 0 && wait_io(ssl, result, deadline));
    return result;
}

void vp_close(SSL *ssl)
{
    SSL_shutdown(ssl);
    SSL_free(ssl);
}

size_t vp_diagnostic(char *buffer, size_t capacity)
{
    unsigned long code = ERR_peek_last_error();
    if (code == 0)
        return 0;
    ERR_error_string_n(code, buffer, capacity);
    return strlen(buffer);
}
