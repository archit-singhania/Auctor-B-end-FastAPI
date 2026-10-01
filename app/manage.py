"""Operator-only role assignment; requires private PostgreSQL connection access."""
import asyncio,sys
from app.db import create_pool,close_pool,get_conn,release_conn
async def main():
    if len(sys.argv)!=3 or sys.argv[1]!='grant-reviewer':raise SystemExit('Usage: python -m app.manage grant-reviewer user@example.com')
    await create_pool();c=await get_conn()
    try:
        row=await c.fetchrow("UPDATE users SET role='reviewer' WHERE email=$1 AND password_hash IS NOT NULL RETURNING id",sys.argv[2].strip().lower())
        if not row:raise SystemExit('Registered account not found')
        await c.execute("INSERT INTO activity(user_id,kind,message) VALUES($1,'role','Operator granted independent reviewer access')",row['id'])
        print('Reviewer role assigned to the registered account')
    finally:await release_conn(c);await close_pool()
if __name__=='__main__':asyncio.run(main())
