--
-- PostgreSQL database dump
--

\restrict lgprAg1xZ8ij8mfztIV8ye6dkWESDIhnHuX2EQiSVfzweOcTm0WZNs31i8IvWDT

-- Dumped from database version 18.6 (653b019)
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: code_symbols; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.code_symbols (
    id bigint NOT NULL,
    repo_id bigint NOT NULL,
    file_id bigint NOT NULL,
    name text NOT NULL,
    qualified_name text NOT NULL,
    kind text NOT NULL,
    signature text,
    docstring text,
    start_line integer NOT NULL,
    end_line integer NOT NULL,
    content_hash text,
    search_vec tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, ((COALESCE(qualified_name, ''::text) || ' '::text) || COALESCE(docstring, ''::text)))) STORED,
    deleted_at timestamp with time zone,
    CONSTRAINT code_symbols_kind_check CHECK ((kind = ANY (ARRAY['function'::text, 'method'::text, 'class'::text, 'module'::text])))
);


ALTER TABLE public.code_symbols OWNER TO neondb_owner;

--
-- Name: code_symbols_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.code_symbols_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.code_symbols_id_seq OWNER TO neondb_owner;

--
-- Name: code_symbols_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.code_symbols_id_seq OWNED BY public.code_symbols.id;


--
-- Name: repo_files; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.repo_files (
    id bigint NOT NULL,
    repo_id bigint NOT NULL,
    path text NOT NULL,
    parent_path text NOT NULL,
    name text NOT NULL,
    node_type text NOT NULL,
    language text,
    description text,
    size_bytes integer,
    content_hash text,
    indexed_sha text,
    deleted_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT repo_files_node_type_check CHECK ((node_type = ANY (ARRAY['file'::text, 'dir'::text])))
);


ALTER TABLE public.repo_files OWNER TO neondb_owner;

--
-- Name: repo_files_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.repo_files_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.repo_files_id_seq OWNER TO neondb_owner;

--
-- Name: repo_files_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.repo_files_id_seq OWNED BY public.repo_files.id;


--
-- Name: repositories; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.repositories (
    id bigint NOT NULL,
    github_repo_id bigint NOT NULL,
    owner text NOT NULL,
    name text NOT NULL,
    default_branch text DEFAULT 'main'::text NOT NULL,
    installation_id bigint,
    last_indexed_sha text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.repositories OWNER TO neondb_owner;

--
-- Name: repositories_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.repositories_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.repositories_id_seq OWNER TO neondb_owner;

--
-- Name: repositories_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.repositories_id_seq OWNED BY public.repositories.id;


--
-- Name: selected_repositories; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.selected_repositories (
    user_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    repo_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.selected_repositories OWNER TO neondb_owner;

--
-- Name: sessions; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.sessions (
    session_id_hash bytea NOT NULL,
    user_id bigint NOT NULL,
    csrf_token text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


ALTER TABLE public.sessions OWNER TO neondb_owner;

--
-- Name: symbol_edges; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.symbol_edges (
    id bigint NOT NULL,
    repo_id bigint NOT NULL,
    source_symbol_id bigint NOT NULL,
    target_symbol_id bigint,
    target_name text NOT NULL,
    relation text NOT NULL,
    call_line integer,
    CONSTRAINT symbol_edges_relation_check CHECK ((relation = ANY (ARRAY['calls'::text, 'imports'::text, 'inherits'::text, 'tests'::text])))
);


ALTER TABLE public.symbol_edges OWNER TO neondb_owner;

--
-- Name: symbol_edges_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.symbol_edges_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.symbol_edges_id_seq OWNER TO neondb_owner;

--
-- Name: symbol_edges_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.symbol_edges_id_seq OWNED BY public.symbol_edges.id;


--
-- Name: user_installations; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.user_installations (
    user_id bigint NOT NULL,
    installation_id bigint NOT NULL,
    account_login text NOT NULL,
    account_id bigint NOT NULL,
    account_type text NOT NULL,
    repository_selection text NOT NULL,
    is_connected boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_installations_account_type_check CHECK ((account_type = ANY (ARRAY['User'::text, 'Organization'::text]))),
    CONSTRAINT user_installations_repository_selection_check CHECK ((repository_selection = ANY (ARRAY['all'::text, 'selected'::text])))
);


ALTER TABLE public.user_installations OWNER TO neondb_owner;

--
-- Name: users; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.users (
    id bigint NOT NULL,
    github_user_id bigint NOT NULL,
    username text NOT NULL,
    email text,
    avatar_url text,
    encrypted_token bytea,
    role text DEFAULT 'user'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_login_at timestamp with time zone
);


ALTER TABLE public.users OWNER TO neondb_owner;

--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.users_id_seq OWNER TO neondb_owner;

--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: code_symbols id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.code_symbols ALTER COLUMN id SET DEFAULT nextval('public.code_symbols_id_seq'::regclass);


--
-- Name: repo_files id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.repo_files ALTER COLUMN id SET DEFAULT nextval('public.repo_files_id_seq'::regclass);


--
-- Name: repositories id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.repositories ALTER COLUMN id SET DEFAULT nextval('public.repositories_id_seq'::regclass);


--
-- Name: symbol_edges id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.symbol_edges ALTER COLUMN id SET DEFAULT nextval('public.symbol_edges_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: code_symbols code_symbols_file_id_qualified_name_kind_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.code_symbols
    ADD CONSTRAINT code_symbols_file_id_qualified_name_kind_key UNIQUE (file_id, qualified_name, kind);


--
-- Name: code_symbols code_symbols_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.code_symbols
    ADD CONSTRAINT code_symbols_pkey PRIMARY KEY (id);


--
-- Name: repo_files repo_files_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.repo_files
    ADD CONSTRAINT repo_files_pkey PRIMARY KEY (id);


--
-- Name: repo_files repo_files_repo_id_path_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.repo_files
    ADD CONSTRAINT repo_files_repo_id_path_key UNIQUE (repo_id, path);


--
-- Name: repositories repositories_github_repo_id_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.repositories
    ADD CONSTRAINT repositories_github_repo_id_key UNIQUE (github_repo_id);


--
-- Name: repositories repositories_owner_name_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.repositories
    ADD CONSTRAINT repositories_owner_name_key UNIQUE (owner, name);


--
-- Name: repositories repositories_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.repositories
    ADD CONSTRAINT repositories_pkey PRIMARY KEY (id);


--
-- Name: selected_repositories selected_repositories_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.selected_repositories
    ADD CONSTRAINT selected_repositories_pkey PRIMARY KEY (user_id, installation_id, repo_id);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (session_id_hash);


--
-- Name: symbol_edges symbol_edges_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.symbol_edges
    ADD CONSTRAINT symbol_edges_pkey PRIMARY KEY (id);


--
-- Name: symbol_edges symbol_edges_source_symbol_id_target_name_relation_call_lin_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.symbol_edges
    ADD CONSTRAINT symbol_edges_source_symbol_id_target_name_relation_call_lin_key UNIQUE (source_symbol_id, target_name, relation, call_line);


--
-- Name: user_installations user_installations_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.user_installations
    ADD CONSTRAINT user_installations_pkey PRIMARY KEY (user_id, installation_id);


--
-- Name: users users_github_user_id_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_github_user_id_key UNIQUE (github_user_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: idx_edge_source; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_edge_source ON public.symbol_edges USING btree (source_symbol_id, relation);


--
-- Name: idx_edge_target; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_edge_target ON public.symbol_edges USING btree (target_symbol_id, relation);


--
-- Name: idx_edge_unresolved; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_edge_unresolved ON public.symbol_edges USING btree (repo_id, target_name) WHERE (target_symbol_id IS NULL);


--
-- Name: idx_files_lang; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_files_lang ON public.repo_files USING btree (repo_id, language);


--
-- Name: idx_files_parent; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_files_parent ON public.repo_files USING btree (repo_id, parent_path);


--
-- Name: idx_files_path_prefix; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_files_path_prefix ON public.repo_files USING btree (repo_id, path text_pattern_ops);


--
-- Name: idx_files_path_trgm; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_files_path_trgm ON public.repo_files USING gin (path public.gin_trgm_ops);


--
-- Name: idx_selected_repo; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_selected_repo ON public.selected_repositories USING btree (repo_id);


--
-- Name: idx_sessions_expires; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_sessions_expires ON public.sessions USING btree (expires_at);


--
-- Name: idx_sessions_user; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_sessions_user ON public.sessions USING btree (user_id);


--
-- Name: idx_sym_file; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_sym_file ON public.code_symbols USING btree (file_id);


--
-- Name: idx_sym_qname_trgm; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_sym_qname_trgm ON public.code_symbols USING gin (qualified_name public.gin_trgm_ops);


--
-- Name: idx_sym_repo_name; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_sym_repo_name ON public.code_symbols USING btree (repo_id, name);


--
-- Name: idx_sym_search; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_sym_search ON public.code_symbols USING gin (search_vec);


--
-- Name: idx_user_inst_installation; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX idx_user_inst_installation ON public.user_installations USING btree (installation_id);


--
-- Name: code_symbols code_symbols_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.code_symbols
    ADD CONSTRAINT code_symbols_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.repo_files(id) ON DELETE CASCADE;


--
-- Name: code_symbols code_symbols_repo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.code_symbols
    ADD CONSTRAINT code_symbols_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.repositories(id) ON DELETE CASCADE;


--
-- Name: repo_files repo_files_repo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.repo_files
    ADD CONSTRAINT repo_files_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.repositories(id) ON DELETE CASCADE;


--
-- Name: selected_repositories selected_repositories_user_id_installation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.selected_repositories
    ADD CONSTRAINT selected_repositories_user_id_installation_id_fkey FOREIGN KEY (user_id, installation_id) REFERENCES public.user_installations(user_id, installation_id) ON DELETE CASCADE;


--
-- Name: sessions sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: symbol_edges symbol_edges_repo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.symbol_edges
    ADD CONSTRAINT symbol_edges_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.repositories(id) ON DELETE CASCADE;


--
-- Name: symbol_edges symbol_edges_source_symbol_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.symbol_edges
    ADD CONSTRAINT symbol_edges_source_symbol_id_fkey FOREIGN KEY (source_symbol_id) REFERENCES public.code_symbols(id) ON DELETE CASCADE;


--
-- Name: symbol_edges symbol_edges_target_symbol_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.symbol_edges
    ADD CONSTRAINT symbol_edges_target_symbol_id_fkey FOREIGN KEY (target_symbol_id) REFERENCES public.code_symbols(id) ON DELETE SET NULL;


--
-- Name: user_installations user_installations_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.user_installations
    ADD CONSTRAINT user_installations_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict lgprAg1xZ8ij8mfztIV8ye6dkWESDIhnHuX2EQiSVfzweOcTm0WZNs31i8IvWDT

