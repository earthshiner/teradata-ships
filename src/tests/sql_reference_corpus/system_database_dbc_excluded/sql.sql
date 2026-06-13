REPLACE VIEW {{DOM_V}}.Audit_Log_V AS
SELECT u.UserName, t.TableName, t.LastAccessTimeStamp
FROM DBC.UsersV u
INNER JOIN DBC.TablesV t ON u.UserName = t.CreatorName
LEFT JOIN {{REF_T}}.Department d ON u.UserName = d.user_name;
