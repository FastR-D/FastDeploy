// Package pgstore opens PostgreSQL from stores that share SQL with SQLite.
package pgstore

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/stdlib"
)

type connector struct{ driver.Connector }

type connection struct{ driver.Conn }

// Open accepts the question-mark parameters used by the portable stores.
// Types and constraints are supplied by each application's PostgreSQL schema.
func Open(dsn string) (*sql.DB, error) {
	config, err := pgx.ParseConfig(dsn)
	if err != nil {
		return nil, errors.New("invalid PostgreSQL storage configuration")
	}
	config.DefaultQueryExecMode = pgx.QueryExecModeSimpleProtocol
	db := sql.OpenDB(connector{stdlib.GetConnector(*config)})
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(4)
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return db, nil
}

func (c connector) Connect(ctx context.Context) (driver.Conn, error) {
	conn, err := c.Connector.Connect(ctx)
	if err != nil {
		return nil, err
	}
	return connection{conn}, nil
}

func (c connection) Prepare(query string) (driver.Stmt, error) {
	return c.Conn.Prepare(rebind(query))
}

func (c connection) PrepareContext(ctx context.Context, query string) (driver.Stmt, error) {
	return c.Conn.(driver.ConnPrepareContext).PrepareContext(ctx, rebind(query))
}

func (c connection) ExecContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Result, error) {
	return c.Conn.(driver.ExecerContext).ExecContext(ctx, rebind(query), args)
}

func (c connection) QueryContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Rows, error) {
	return c.Conn.(driver.QueryerContext).QueryContext(ctx, rebind(query), args)
}

func (c connection) CheckNamedValue(v *driver.NamedValue) error {
	// The common schemas retain SQLite's integer representation of booleans.
	if boolean, ok := v.Value.(bool); ok {
		v.Value = int64(0)
		if boolean {
			v.Value = int64(1)
		}
	}
	return c.Conn.(driver.NamedValueChecker).CheckNamedValue(v)
}

func (c connection) BeginTx(ctx context.Context, options driver.TxOptions) (driver.Tx, error) {
	tx, err := c.Conn.(driver.ConnBeginTx).BeginTx(ctx, options)
	if err != nil {
		return nil, err
	}
	// These stores use transactions for read-modify-write operations. Carry over
	// the single-writer ordering of SQLite across processes sharing this database.
	if !options.ReadOnly {
		_, err = c.Conn.(driver.ExecerContext).ExecContext(ctx, "SELECT pg_advisory_xact_lock(307381225)", nil)
		if err != nil {
			_ = tx.Rollback()
			return nil, err
		}
	}
	return tx, nil
}

func (c connection) Ping(ctx context.Context) error {
	return c.Conn.(driver.Pinger).Ping(ctx)
}

func (c connection) ResetSession(ctx context.Context) error {
	return c.Conn.(driver.SessionResetter).ResetSession(ctx)
}

func (c connection) IsValid() bool {
	return c.Conn.(driver.Validator).IsValid()
}

func rebind(query string) string {
	if !strings.Contains(query, "?") {
		return query
	}
	var output strings.Builder
	output.Grow(len(query))
	var quote byte
	number := 0
	for i := 0; i < len(query); i++ {
		ch := query[i]
		if quote != 0 {
			output.WriteByte(ch)
			if ch == quote {
				if i+1 < len(query) && query[i+1] == quote {
					i++
					output.WriteByte(query[i])
				} else {
					quote = 0
				}
			}
			continue
		}
		switch ch {
		case '\'', '"':
			quote = ch
			output.WriteByte(ch)
		case '?':
			number++
			output.WriteByte('$')
			output.WriteString(strconv.Itoa(number))
		default:
			output.WriteByte(ch)
		}
	}
	return output.String()
}
