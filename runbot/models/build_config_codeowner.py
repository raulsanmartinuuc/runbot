import re
from odoo import models, fields


class ConfigStep(models.Model):
    _inherit = 'runbot.build.config.step'

    job_type = fields.Selection(selection_add=[('codeowner', 'Codeowner')], ondelete={'codeowner': 'cascade'})
    fallback_reviewer = fields.Char('Fallback reviewer')

    def _pr_by_commit(self, build, prs):
        pr_by_commit = {}
        for commit_link in build.params_id.commit_link_ids:
            commit = commit_link.commit_id
            repo_pr = prs.filtered(lambda pr: pr.remote_id.repo_id == commit_link.commit_id.repo_id)
            if repo_pr:
                if len(repo_pr) > 1:
                    build._log('', 'More than one open pr in this bundle for %s: %s' % (commit.repo_id.name, [pr.name for pr in repo_pr]), level='ERROR')
                    build.local_result = 'ko'
                    return {}
                build._log('', 'PR [%s](%s) found for repo **%s**' % (repo_pr.dname, repo_pr.branch_url, commit.repo_id.name), log_type='markdown')
                pr_by_commit[commit_link] = repo_pr
            else:
                build._log('', 'No pr for repo %s, skipping' % commit.repo_id.name)
        return pr_by_commit
    
    def _get_module(self, repo, file):
        for addons_path in repo.addons_paths.split(','):
            base_path = f'{repo.name}/{addons_path}'
            if file.startswith(base_path):
                return file.replace(base_path, '').strip('/').split('/')[0]

    def _codeowners_regexes(self, codeowners, version_id):
        regexes = {}
        for codeowner in codeowners:
            if codeowner.github_teams and codeowner.regex and (codeowner._match_version(version_id)):
                team_set = regexes.setdefault(codeowner.regex.strip(), set()) 
                team_set |= set(t.strip() for t in codeowner.github_teams.split(','))
        return list(regexes.items())

    def _reviewer_per_file(self, files, regexes, ownerships, repo):
        reviewer_per_file = {}
        for file in files:
            file_reviewers = set()
            for regex, teams in regexes:
                if re.match(regex, file):
                    if not teams or 'none' in teams:
                        file_reviewers = set()
                        break # blacklisted, break
                    file_reviewers |= teams

            file_module = self._get_module(repo, file)
            for ownership in ownerships:
                if file_module == ownership.module_id.name and not ownership.is_fallback and ownership.team_id.github_team not in file_reviewers:
                    file_reviewers.add(ownership.team_id.github_team)
            # fallback
            if not file_reviewers:
                for ownership in ownerships:
                    if file_module == ownership.module_id.name:
                        file_reviewers.add(ownership.team_id.github_team)
            if not file_reviewers and self.fallback_reviewer:
                file_reviewers.add(self.fallback_reviewer)
            reviewer_per_file[file] = file_reviewers
        return reviewer_per_file
    
    def _run_codeowner(self, build, log_path):
        bundle = build.params_id.create_batch_id.bundle_id
        if bundle.is_base:
            build._log('', 'Skipping base bundle')
            return
   
        if not self._check_limits(build):
            return

        prs = bundle.branch_ids.filtered(lambda branch: branch.is_pr and branch.alive)

        # skip draft pr
        draft_prs = prs.filtered(lambda pr: pr.draft)
        if draft_prs:
            build._log('', 'Some pr are draft, skipping: %s' % ','.join([pr.name for pr in draft_prs]), level='WARNING')
            build.local_result = 'warn'
            return

        # remove forwardport pr
        ICP = self.env['ir.config_parameter'].sudo()

        fw_bot = ICP.get_param('runbot.runbot_forwardport_author')
        fw_prs = prs.filtered(lambda pr: pr.pr_author == fw_bot and len(pr.reflog_ids) <= 1)
        if fw_prs:
            build._log('', 'Ignoring forward port pull request: %s' % ','.join([pr.name for pr in fw_prs]))
            prs = list(set(prs) - set(fw_prs))

        if not prs:
            return

        # check prs targets
        valid_targets = set([(branch.remote_id, branch.name) for branch in bundle.base_id.branch_ids])
        invalid_target_prs = prs.filtered(lambda pr: (pr.remote_id, pr.target_branch_name) not in valid_targets)

        if invalid_target_prs:
            # this is not perfect but detects prs inside odoo-dev or with invalid target
            build._log('', 'Some pr have an invalid target: %s' % ','.join([pr.name for pr in invalid_target_prs]), level='ERROR')
            build.local_result = 'ko'
            return

        build._checkout()

        pr_by_commit = self._pr_by_commit(build, prs)
        ownerships = self.env['runbot.module.ownership'].search([('team_id.github_team', '!=', False)])
        codeowners = build.env['runbot.codeowner'].search([('project_id', '=', bundle.project_id.id)])
        regexes = self._codeowners_regexes(codeowners, build.params_id.version_id)
        modified_files = self._modified_files(build, pr_by_commit.keys())
        
        for commit_link, files in modified_files.items():
            build._log('','Checking %s codeowner regexed on %s files' % (len(regexes), len(files)))
            
            reviewers = set()
            reviewer_per_file = self._reviewer_per_file(files, regexes, ownerships, commit_link.commit_id.repo_id)
            for file, file_reviewers in reviewer_per_file.items():
                href = 'https://%s/blob/%s/%s' % (commit_link.branch_id.remote_id.base_url, commit_link.commit_id.name, file.split('/', 1)[-1])
                build._log('', 'Adding %s to reviewers for file [%s](%s)' % (', '.join(sorted(file_reviewers)), file, href), log_type='markdown')
                reviewers |= file_reviewers

            if reviewers:
                pr = pr_by_commit[commit_link]
                new_reviewers = sorted(reviewers - set((pr.reviewers or '').split(',')))
                if new_reviewers:
                    build._log('', 'Requesting review for pull request [%s](%s): %s' % (pr.dname, pr.branch_url, ', '.join(new_reviewers)), log_type='markdown')
                    response = pr.remote_id._github('/repos/:owner/:repo/pulls/%s/requested_reviewers' % pr.name, {"team_reviewers":list(new_reviewers)}, ignore_errors=False)
                    pr._compute_branch_infos(response)
                    pr['reviewers'] = ','.join(sorted(reviewers))
                else:
                    build._log('', 'All reviewers are already on pull request [%s](%s)' % (pr.dname, pr.branch_url,), log_type='markdown')

