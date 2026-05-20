CREATE DATABASE providex_test OWNER providex;
\connect providex_test
CREATE EXTENSION IF NOT EXISTS timescaledb;
\connect providex_dev
CREATE EXTENSION IF NOT EXISTS timescaledb;
